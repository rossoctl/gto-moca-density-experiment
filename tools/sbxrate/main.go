// sbxrate: a minimal direct sandbox/v1 exec client (the equivalent of
// `mocactl sbx run` without the Node.js process, per-exec token mint, and
// per-exec TLS handshake).
//
// Protocol (proto/sandbox/v1/sandbox.proto):
//   gRPC server-streaming: POST /sandbox.v1.SandboxExec/Exec
//   ExecRequest{ sandbox_id=1, exec=2{ command=2, stdin=3, timeout_s=4, workspace_key=6 } }
//   ExecEvent{ chunk=1 | end=2{ req_id=1, exit_code=2 sint32, truncated=3 } | error=3{ req_id=1, message=2 } }
//
// Auth: HTTP header "authorization: Bearer <sandbox token>" (minted once
// outside this tool, e.g. `mocactl sbx token <id>`; it lives 15 minutes).
//
// Usage:
//   sbxrate -addr 192.168.18.206:9444 -ca exec-ca.crt -token TOKEN \
//           -sid density_default_256 -wsk sbx-... [-workers 21] [-window 60s] [-cmd true]
package main

import (
	"bufio"
	"bytes"
	"context"
	crand "crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"

	"golang.org/x/net/http2"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ---- minimal protobuf encoding ----

func putTag(buf *bytes.Buffer, field int, wire int) {
	putUvarint(buf, uint64(field)<<3|uint64(wire))
}

func putUvarint(buf *bytes.Buffer, v uint64) {
	var tmp [binary.MaxVarintLen64]byte
	n := binary.PutUvarint(tmp[:], v)
	buf.Write(tmp[:n])
}

func str(field int, s string) []byte {
	var b bytes.Buffer
	putTag(&b, field, 2)
	putUvarint(&b, uint64(len(s)))
	b.WriteString(s)
	return b.Bytes()
}

func msg(field int, inner []byte) []byte {
	var b bytes.Buffer
	putTag(&b, field, 2)
	putUvarint(&b, uint64(len(inner)))
	b.Write(inner)
	return b.Bytes()
}

func varintField(field int, v uint64) []byte {
	var b bytes.Buffer
	putTag(&b, field, 0)
	putUvarint(&b, v)
	return b.Bytes()
}

// zigzag for sint32
func zigzag32(v int32) uint64 { return uint64((v << 1) ^ (v >> 31)) }

// ---- minimal protobuf decoding (just what ExecEvent needs) ----

type execEvent struct {
	kind     int // 0 unknown, 1 chunk, 2 end, 3 error
	exitCode *int32
	errMsg   string
}

func decodeExecEvent(b []byte) execEvent {
	ev := execEvent{}
	for len(b) > 0 {
		tag, n := binary.Uvarint(b)
		if n <= 0 {
			break
		}
		b = b[n:]
		field, wire := int(tag>>3), int(tag&7)
		switch wire {
		case 0:
			v, n := binary.Uvarint(b)
			if n <= 0 {
				return ev
			}
			b = b[n:]
			if field == 2 { // End.exit_code (sint32)
				z := int32(v >> 1)
				if v&1 == 1 {
					z = ^z
				}
				ev.exitCode = &z
			}
		case 2:
			l, n := binary.Uvarint(b)
			if n <= 0 {
				return ev
			}
			b = b[n:]
			if int(l) > len(b) {
				return ev
			}
			payload := b[:l]
			b = b[l:]
			if field == 1 {
				ev.kind = 1 // chunk
			} else if field == 2 {
				ev.kind = 2 // end
				ev2 := decodeExecEvent(payload) // reuse: parse End fields
				ev.exitCode = ev2.exitCode
			} else if field == 3 {
				ev.kind = 3
				ev.errMsg = string(payload) // good enough for diagnostics
			}
		default:
			return ev // length-delimited only
		}
	}
	return ev
}

// parse End message for exit_code
func init() {}

// decodeEnd walks an End message { req_id=1 varint, exit_code=2 sint32, truncated=3 bool }
func decodeEnd(b []byte) *int32 {
	var code *int32
	for len(b) > 0 {
		tag, n := binary.Uvarint(b)
		if n <= 0 {
			break
		}
		b = b[n:]
		field, wire := int(tag>>3), int(tag&7)
		if wire == 0 {
			v, n := binary.Uvarint(b)
			if n <= 0 {
				break
			}
			b = b[n:]
			if field == 2 {
				z := int32(v >> 1)
				if v&1 == 1 {
					z = ^z
				}
				code = &z
			}
		} else if wire == 2 {
			l, n := binary.Uvarint(b)
			if n <= 0 {
				break
			}
			b = b[n:]
			if int(l) > len(b) {
				break
			}
			b = b[l:]
		}
	}
	return code
}

// decodeErr walks an ExecError message { req_id=1, message=2 }
func decodeErr(b []byte) string {
	for len(b) > 0 {
		tag, n := binary.Uvarint(b)
		if n <= 0 {
			break
		}
		b = b[n:]
		field, wire := int(tag>>3), int(tag&7)
		if wire == 2 {
			l, n := binary.Uvarint(b)
			if n <= 0 {
				break
			}
			b = b[n:]
			if int(l) > len(b) {
				break
			}
			if field == 2 {
				return string(b[:l])
			}
			b = b[l:]
		} else if wire == 0 {
			_, n := binary.Uvarint(b)
			if n <= 0 {
				break
			}
			b = b[n:]
		}
	}
	return ""
}

// req_id: salt+counter under 2^32 — the relay is Node.js, where a uint64
// above 2^53 loses precision, and it dedups in-flight execs by req_id, so a
// fixed 0 makes concurrent execs on one sandbox refuse each other.
var reqIDSalt = randU64()
var reqIDCounter uint64

func randU64() uint64 {
	var b [8]byte
	if _, err := crand.Read(b[:]); err != nil {
		return uint64(time.Now().UnixNano())
	}
	return binary.LittleEndian.Uint64(b[:])
}

func nextReqID() uint64 {
	return (reqIDSalt&0x00FFFFFF)<<8 | (atomic.AddUint64(&reqIDCounter, 1) & 0xFF)
}

func buildExecRequest(sandboxID, workspaceKey, cmd, stdin string, timeoutS uint32) []byte {
	var exec bytes.Buffer
	exec.Write(varintField(1, nextReqID()))    // req_id
	exec.Write(str(2, cmd))            // command
	exec.Write(str(3, stdin))           // stdin
	exec.Write(varintField(4, uint64(timeoutS))) // timeout_s
	exec.Write(str(6, workspaceKey))    // workspace_key (microvm refuses empty)

	var req bytes.Buffer
	req.Write(str(1, sandboxID)) // sandbox_id
	req.Write(msg(2, exec.Bytes()))
	return req.Bytes()
}

// gRPC frame: 1-byte compressed flag + 4-byte big-endian length + message
func grpcFrame(pb []byte) []byte {
	out := make([]byte, 5, 5+len(pb))
	out[0] = 0
	binary.BigEndian.PutUint32(out[1:5], uint32(len(pb)))
	return append(out, pb...)
}

func main() {
	addr := flag.String("addr", "", "exec relay addr host:port")
	caFile := flag.String("ca", "", "exec CA PEM file")
	token := flag.String("token", "", "sandbox token (Bearer)")
	sid := flag.String("sid", "", "sandbox id (worker id, e.g. density_default_256)")
	wsk := flag.String("wsk", "", "workspace key (the sbx-... id)")
	pairsFile := flag.String("pairs", "", "file with lines '<workspace_key> <token>' (multiple sandboxes; workers spread round-robin)")
	workers := flag.Int("workers", 1, "concurrent exec loops")
	window := flag.Duration("window", 60*time.Second, "measurement window")
	cmd := flag.String("cmd", "true", "command to run")
	insecure := flag.Bool("insecure-tls", false, "skip TLS verification (still encrypted)")
	flag.Parse()

	type sbxSpec struct{ wsk, token string }
	var sandboxes []sbxSpec
	if *pairsFile != "" {
		f, err := os.Open(*pairsFile)
		if err != nil {
			fmt.Fprintln(os.Stderr, "read pairs:", err)
			os.Exit(1)
		}
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			line := strings.TrimSpace(sc.Text())
			if line == "" {
				continue
			}
			var w, t string
			if _, err := fmt.Sscanf(line, "%s %s", &w, &t); err != nil || w == "" || t == "" {
				fmt.Fprintln(os.Stderr, "bad pairs line:", line)
				os.Exit(1)
			}
			sandboxes = append(sandboxes, sbxSpec{w, t})
		}
		f.Close()
		if *sid == "" {
			fmt.Fprintln(os.Stderr, "-pairs also needs -sid (the worker/template id)")
			os.Exit(2)
		}
	} else {
		if *addr == "" || *token == "" || *sid == "" || *wsk == "" {
			fmt.Fprintln(os.Stderr, "need -addr -token -sid -wsk (-ca), or -pairs + -sid")
			os.Exit(2)
		}
		sandboxes = []sbxSpec{{*wsk, *token}}
	}

	tlsCfg := &tls.Config{InsecureSkipVerify: *insecure, NextProtos: []string{"h2"}}
	if *caFile != "" {
		// comma-separated PEM files (one per sandbox) all go into one pool
		pool := x509.NewCertPool()
		for _, f := range strings.Split(*caFile, ",") {
			f = strings.TrimSpace(f)
			if f == "" {
				continue
			}
			pem, err := os.ReadFile(f)
			if err != nil {
				fmt.Fprintln(os.Stderr, "read ca:", err)
				os.Exit(1)
			}
			if !pool.AppendCertsFromPEM(pem) {
				fmt.Fprintln(os.Stderr, "warn: no certs parsed from", f)
			}
		}
		tlsCfg.RootCAs = pool
	}

	// The relay's TLS endpoint (via the socat proxy) does not negotiate ALPN,
	// so net/http would fall back to HTTP/1.1. x/net/http2 with a custom
	// DialTLSContext speaks h2 with prior knowledge and skips the ALPN check.
	transport := &http2.Transport{
		TLSClientConfig: tlsCfg,
		DialTLSContext: func(ctx context.Context, network, addr string, cfg *tls.Config) (net.Conn, error) {
			c := cfg.Clone()
			c.NextProtos = []string{"h2"}
			d := &net.Dialer{}
			raw, err := d.DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}
			host, _, err := net.SplitHostPort(addr)
			if err == nil && c.ServerName == "" {
				c.ServerName = host
			}
			tc := tls.Client(raw, c)
			if err := tc.HandshakeContext(ctx); err != nil {
				raw.Close()
				return nil, err
			}
			return tc, nil
		},
	}
	client := &http.Client{Transport: transport}

	// warm one exec to fail fast on protocol errors
	if code, _, err := oneExec(client, *addr, sandboxes[0].token, *sid, sandboxes[0].wsk, *cmd); err != nil {
		fmt.Fprintln(os.Stderr, "warmup exec failed:", err)
		os.Exit(1)
	} else if code != 0 {
		fmt.Fprintf(os.Stderr, "warmup exec exit=%d\n", code)
		os.Exit(1)
	}

	var ok, fail int64
	var mu sync.Mutex
	var lats []float64
	stop := time.Now().Add(*window)

	cpuBefore := readProcStat()
	t0 := time.Now()

	var wg sync.WaitGroup
	for w := 0; w < *workers; w++ {
		wg.Add(1)
		spec := sandboxes[w%len(sandboxes)]
		go func() {
			defer wg.Done()
			var local []float64
			var lok, lfail int64
			for time.Now().Before(stop) {
				s := time.Now()
				code, _, err := oneExec(client, *addr, spec.token, *sid, spec.wsk, *cmd)
				dt := time.Since(s).Seconds()
				if err == nil && code == 0 {
					lok++
					local = append(local, dt)
				} else {
					lfail++
				}
			}
			mu.Lock()
			ok += lok
			fail += lfail
			lats = append(lats, local...)
			mu.Unlock()
		}()
	}
	wg.Wait()
	wall := time.Since(t0).Seconds()
	cpuS := readProcStat() - cpuBefore

	sort.Float64s(lats)
	fmt.Printf("sbxrate: %d workers, %.0fs window, cmd=%q\n", *workers, wall, *cmd)
	fmt.Printf("  %d SUCCEEDED, %d FAILED\n", ok, fail)
	fmt.Printf("  rate: %.1f execs/s\n", float64(ok)/wall)
	if len(lats) > 0 {
		fmt.Printf("  latency: p50 %.3fs p95 %.3fs\n", lats[len(lats)/2], lats[int(float64(len(lats))*0.95)])
	}
	fmt.Printf("  host CPU: %.1fs = %.1f cores\n", cpuS, cpuS/wall)
	if ok > 0 {
		fmt.Printf("  host CPU per exec: %.0fms\n", cpuS/float64(ok)*1000)
	}
}

func oneExec(client *http.Client, addr, token, sid, wsk, cmd string) (int32, string, error) {
	pb := buildExecRequest(sid, wsk, cmd, "", 30)
	body := bytes.NewReader(grpcFrame(pb))
	req, err := http.NewRequest("POST", "https://"+addr+"/sandbox.v1.SandboxExec/Exec", body)
	if err != nil {
		return 0, "", err
	}
	req.Header.Set("content-type", "application/grpc")
	req.Header.Set("te", "trailers")
	req.Header.Set("authorization", "Bearer "+token)
	req.Header.Set("grpc-timeout", "60S")

	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	req = req.WithContext(ctx)

	resp, err := client.Do(req)
	if err != nil {
		return 0, "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return 0, "", fmt.Errorf("http %s", resp.Status)
	}

	br := bufio.NewReader(resp.Body)
	var exit int32
	sawEnd := false
	for {
		hdr := make([]byte, 5)
		if _, err := io.ReadFull(br, hdr); err != nil {
			if err == io.EOF && sawEnd {
				return exit, "", nil
			}
			return 0, "", fmt.Errorf("stream: %w", err)
		}
		n := binary.BigEndian.Uint32(hdr[1:5])
		msgBytes := make([]byte, n)
		if _, err := io.ReadFull(br, msgBytes); err != nil {
			return 0, "", fmt.Errorf("body: %w", err)
		}
		switch decodeExecEvent(msgBytes).kind {
		case 2: // End
			if c := decodeEnd(msgBytes); c != nil {
				exit = *c
			}
			sawEnd = true
			// keep reading until EOF so trailers are processed
		case 3: // ExecError
			return 0, decodeErr(msgBytes), fmt.Errorf("ExecError")
		}
	}
}

// ---- host CPU accounting (same as bench-template.py: /proc/stat, idle excluded) ----

func readProcStat() float64 {
	f, err := os.Open("/proc/stat")
	if err != nil {
		return 0
	}
	defer f.Close()
	var cpu, idle string
	var vals []int
	var total int64
	fmt.Fscan(f, &cpu)
	for i := 0; i < 10; i++ {
		var v int
		if nn, _ := fmt.Fscan(f, &v); nn == 0 {
			break
		}
		vals = append(vals, v)
	}
	_ = idle
	for _, v := range vals {
		total += int64(v)
	}
	idleSum := int64(0)
	if len(vals) > 3 {
		idleSum = int64(vals[3])
	}
	if len(vals) > 4 {
		idleSum += int64(vals[4])
	}
	return float64(total-idleSum) / 100.0
}
