// sbxrun: run one command in a moca sandbox, with mocactl-sbx-run semantics
// but without mocactl's per-invocation cost (a Node.js process, a token mint
// and a TLS handshake per exec).
//
// It is a drop-in for the exec hot path:
//
//	mocactl sbx run --sandbox ID --timeout S [-- CMD ...]   (script on stdin)
//	sbxrun         --sandbox ID --timeout S [-- CMD ...]    (script on stdin)
//
// Exit codes match mocactl: the command's own code; 124 = timed out or
// killed; 125 = sandbox/machinery failure. stdout and stderr of the command
// are written to this process's stdout and stderr.
//
// Self-configuring: given --sandbox ID it mints a sandbox token through the
// control plane (the mint response carries the exec endpoint, the workspace
// key and the exec CA), and caches the token on disk for its lifetime, so
// consecutive invocations pay neither the mint nor (thanks to HTTP/2
// keep-alive within one invocation, and a fresh but cheap TLS handshake
// between invocations) anything like mocactl's startup cost.
//
// Control plane discovery: --control-plane-url, $MOCACTL_SBX_URL, or the
// controlPlaneUrl remembered in ~/.config/mocactl/sbx-auth.json (same file
// mocactl uses; its apiToken is used for the mint, refreshed through
// POST /v1/auth/token when expired, and written back).
//
// Wire protocol: proto/sandbox/v1/sandbox.proto in the moca repo —
// gRPC server-streaming POST /sandbox.v1.SandboxExec/Exec over TLS, h2 with
// prior knowledge (the relay does not negotiate ALPN), authorization: Bearer.
package main

import (
	"bufio"
	"bytes"
	"context"
	crand "crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os/exec"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"golang.org/x/net/http2"
)

const (
	exitTimeout   = 124 // command timed out or was killed (mocactl semantics)
	exitMachinery = 125 // sandbox failure (mocactl semantics)
)

// ---- minimal protobuf encoding ----

func putUvarint(buf *bytes.Buffer, v uint64) {
	var tmp [binary.MaxVarintLen64]byte
	n := binary.PutUvarint(tmp[:], v)
	buf.Write(tmp[:n])
}

func putTag(buf *bytes.Buffer, field, wire int) {
	putUvarint(buf, uint64(field)<<3|uint64(wire))
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

// reqID: salt + counter, unique per process ("salt+counter, unique per
// process, probabilistically across them" — the relay dedups in-flight execs
// by req_id, so a fixed 0 makes concurrent execs on one sandbox refuse each
// other with req_id_in_flight).
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
	// salt+counter, kept under 2^32: the relay is Node.js, where a uint64
	// above 2^53 loses precision in JS numbers (a large salt made every exec
	// die silently before the relay's audit log). 24-bit salt distinguishes
	// processes, 8-bit counter distinguishes execs within one process.
	return (reqIDSalt&0x00FFFFFF)<<8 | (atomic.AddUint64(&reqIDCounter, 1) & 0xFF)
}

func buildExecRequest(sandboxID, workspaceKey, cmd string, stdin []byte, timeoutS uint32) []byte {
	var exec bytes.Buffer
	exec.Write(varintField(1, nextReqID()))      // req_id
	exec.Write(str(2, cmd))                      // command
	exec.Write(str(3, string(stdin)))             // stdin (protobuf bytes)
	exec.Write(varintField(4, uint64(timeoutS)))  // timeout_s
	exec.Write(str(6, workspaceKey))              // workspace_key (microvm refuses empty)

	var req bytes.Buffer
	req.Write(str(1, sandboxID)) // sandbox_id
	req.Write(msg(2, exec.Bytes()))
	return req.Bytes()
}

func grpcFrame(pb []byte) []byte {
	out := make([]byte, 5, 5+len(pb))
	out[0] = 0
	binary.BigEndian.PutUint32(out[1:5], uint32(len(pb)))
	return append(out, pb...)
}

// ---- minimal protobuf decoding: ExecEvent { chunk=1 | end=2 | error=3 } ----

type wireField struct {
	field, wire int
	varint      uint64
	bytes       []byte
	ok          bool
}

func walk(b []byte, fn func(wireField)) {
	for len(b) > 0 {
		tag, n := binary.Uvarint(b)
		if n <= 0 {
			return
		}
		b = b[n:]
		w := wireField{field: int(tag >> 3), wire: int(tag & 7)}
		switch w.wire {
		case 0:
			v, n := binary.Uvarint(b)
			if n <= 0 {
				return
			}
			b = b[n:]
			w.varint, w.ok = v, true
		case 2:
			l, n := binary.Uvarint(b)
			if n <= 0 || int(l) > len(b) {
				return
			}
			b = b[n:]
			w.bytes, w.ok = b[:l], true
			b = b[l:]
		default:
			return
		}
		fn(w)
	}
}

// endExitCode decodes End{ req_id=1, exit_code=2 sint32, truncated=3 }
func endExitCode(b []byte) (int32, bool) {
	var code int32
	found := false
	walk(b, func(w wireField) {
		if w.field == 2 && w.wire == 0 {
			z := int32(w.varint >> 1)
			if w.varint&1 == 1 {
				z = ^z
			}
			code, found = z, true
		}
	})
	return code, found
}

// chunkData decodes Chunk{ req_id=1, data=2, stream=3 }; returns data and
// whether it is stderr (Stream=2).
func chunkData(b []byte) ([]byte, bool) {
	var data []byte
	stderr := false
	walk(b, func(w wireField) {
		if w.field == 2 && w.wire == 2 {
			data = w.bytes
		} else if w.field == 3 && w.wire == 0 {
			stderr = w.varint == 2
		}
	})
	return data, stderr
}

// errMessage decodes ExecError{ req_id=1, message=2 }
func errMessage(b []byte) string {
	m := ""
	walk(b, func(w wireField) {
		if w.field == 2 && w.wire == 2 {
			m = string(w.bytes)
		}
	})
	return m
}

// ---- control plane: sandbox token mint, with on-disk cache and refresh ----

type sbxToken struct {
	Token        string `json:"token"`
	ExpiresAt    int64  `json:"expiresAt"`
	ExecEndpoint string `json:"execEndpoint"`
	Wrk          string `json:"wrk"`
	Wsk          string `json:"wsk"`
	ExecCa       string `json:"execCa"`
}

// tokenCachePath: one cached token per sandbox.
func tokenCachePath(sandboxID string) string {
	base, err := os.UserCacheDir()
	if err != nil {
		if home, e := os.UserHomeDir(); e == nil {
			base = filepath.Join(home, ".cache")
		} else {
			return ""
		}
	}
	return filepath.Join(base, "sbxrun", "token-"+sandboxID+".json")
}

// getSandboxToken: cached mint; force re-mints (after UNAUTHENTICATED).
//
// The mint goes through mocactl (`mocactl sbx token --json`), NOT the
// control plane directly. The login's refresh token ROTATES on every
// refresh; sbx-auth.json is shared state, and a second client refreshing
// it concurrently presents a superseded token, which the control plane
// reads as theft and revokes the whole family (audit:
// refresh_reuse_detected — this killed two logins before the cause was
// found). mocactl is the single owner of the login; sbxrun never touches
// sbx-auth.json. The cost is one Node process per sandbox per 15-minute
// token life — amortized over the execs it serves, negligible.
func getSandboxToken(sandboxID string, force bool) (*sbxToken, error) {
	cache := tokenCachePath(sandboxID)
	if !force && cache != "" {
		if b, err := os.ReadFile(cache); err == nil {
			var t sbxToken
			if json.Unmarshal(b, &t) == nil && time.Now().Unix()+120 < t.ExpiresAt {
				return &t, nil
			}
		}
	}
	var t sbxToken
	out, err := runMocactl("token", "--json", sandboxID)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(out, &t); err != nil || t.Token == "" {
		return nil, fmt.Errorf("mocactl sbx token --json: unparsable output: %.200s", out)
	}
	if cache != "" {
		os.MkdirAll(filepath.Dir(cache), 0700)
		os.WriteFile(cache, mustJSON(t), 0600)
	}
	return &t, nil
}

// runMocactl runs the mocactl CLI and returns its stdout. Path:
// $SBXRUN_MOCACTL, else "mocactl" from PATH, else ~/bin/mocactl. The
// environment passes through, so MOCACTL_SBX_URL controls the control
// plane and mocactl refreshes the login when it must.
func runMocactl(args ...string) ([]byte, error) {
	exe := os.Getenv("SBXRUN_MOCACTL")
	if exe == "" {
		if p, err := exec.LookPath("mocactl"); err == nil {
			exe = p
		} else if home, e := os.UserHomeDir(); e == nil {
			exe = filepath.Join(home, "bin", "mocactl")
		}
	}
	cmd := exec.Command(exe, append([]string{"sbx"}, args...)...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("mocactl sbx %s: %v: %.300s", args[0], err, stderr.String())
	}
	return out, nil
}

func mustJSON(v any) []byte {
	b, _ := json.Marshal(v)
	return b
}

// ---- the exec ----

func oneExec(client *http.Client, addr, token, sandboxID, wsk, cmd string, stdin []byte, timeoutS uint32, out, errw io.Writer) (int32, error) {
	pb := buildExecRequest(sandboxID, wsk, cmd, stdin, timeoutS)
	if os.Getenv("SBXRUN_DUMP") != "" {
		fmt.Fprintf(os.Stderr, "req hex: %x\n", pb)
	}
	scheme := "https://"
	if strings.HasPrefix(addr, "h2c://") {
		scheme, addr = "http://", strings.TrimPrefix(addr, "h2c://")
	}
	req, err := http.NewRequest("POST", scheme+addr+"/sandbox.v1.SandboxExec/Exec",
		bytes.NewReader(grpcFrame(pb)))
	if err != nil {
		return 0, err
	}
	req.Header.Set("content-type", "application/grpc")
	req.Header.Set("te", "trailers")
	req.Header.Set("authorization", "Bearer "+token)
	req.Header.Set("grpc-timeout", "120S")

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Second)
	defer cancel()
	req = req.WithContext(ctx)

	resp, err := client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return 0, fmt.Errorf("http %s", resp.Status)
	}
	// a trailers-only gRPC error (e.g. RESOURCE_EXHAUSTED "sandbox busy")
	// carries grpc-status in the response HEADERS, with no message frames
	if st := resp.Header.Get("Grpc-Status"); st != "" && st != "0" {
		msg, _ := url.QueryUnescape(resp.Header.Get("Grpc-Message"))
		return 0, fmt.Errorf("grpc status %s: %s", st, msg)
	}

	br := bufio.NewReader(resp.Body)
	for {
		hdr := make([]byte, 5)
		if _, err := io.ReadFull(br, hdr); err != nil {
			// a non-OK gRPC status arrives in trailers, not a frame: surface
			// it instead of a bare EOF (e.g. RESOURCE_EXHAUSTED "sandbox busy")
			if st := resp.Trailer.Get("Grpc-Status"); st != "" && st != "0" {
				desc := resp.Trailer.Get("Grpc-Message")
				return 0, fmt.Errorf("grpc status %s: %s", st, desc)
			}
			return 0, fmt.Errorf("stream: %w", err)
		}
		n := binary.BigEndian.Uint32(hdr[1:5])
		body := make([]byte, n)
		if _, err := io.ReadFull(br, body); err != nil {
			return 0, fmt.Errorf("body: %w", err)
		}
		done := false
		var result int32
		var errMsg string
		walk(body, func(w wireField) {
			switch w.field {
			case 1: // chunk
				if data, stderr := chunkData(w.bytes); len(data) > 0 {
					if stderr {
						errw.Write(data)
					} else {
						out.Write(data)
					}
				}
			case 2: // end
				if c, ok := endExitCode(w.bytes); ok {
					result = c
				}
				done = true
			case 3: // ExecError{req_id=1, message=2} — w.bytes is the payload
				errMsg = errMessage(w.bytes)
				done = true
				result = -999 // sentinel: machinery error
			}
		})
		if done {
			if result == -999 {
				return 0, fmt.Errorf("ExecError: %s", errMsg)
			}
			return result, nil
		}
	}
}

// newExecClient: TLS + h2 prior knowledge (no ALPN on the relay's endpoint).
// An endpoint of the form "h2c://host:port" dials plaintext HTTP/2 instead
// (A/B testing the exec route; e.g. the relay's own listener behind the socat
// TLS proxy).
func newExecClient(caPem, endpoint string) (*http.Client, error) {
	h2c := strings.HasPrefix(endpoint, "h2c://")
	tlsCfg := &tls.Config{NextProtos: []string{"h2"}}
	if caPem != "" && !h2c {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM([]byte(caPem)) {
			return nil, errors.New("no certs in exec CA PEM")
		}
		tlsCfg.RootCAs = pool
	}
	transport := &http2.Transport{
		AllowHTTP:      h2c,
		TLSClientConfig: tlsCfg,
		DialTLSContext: func(ctx context.Context, network, addr string, cfg *tls.Config) (net.Conn, error) {
			d := &net.Dialer{}
			raw, err := d.DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}
			if h2c {
				return raw, nil // plaintext h2, prior knowledge
			}
			c := cfg.Clone()
			c.NextProtos = []string{"h2"}
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
	_ = transport
	return &http.Client{Transport: transport}, nil
}

func main() {
	sandbox := flag.String("sandbox", "", "sandbox id (sbx-...)")
	timeout := flag.Int("timeout", 0, "exec timeout seconds (0: server default)")
	cpURL := flag.String("control-plane-url", "", "control plane URL (default: $MOCACTL_SBX_URL, then sbx-auth.json)")
	cmd := stringFlag("c", "bash", "command to run (default: bash, reading the script on stdin)")
	verbose := flag.Bool("v", false, "print timing to stderr")
	flag.Usage = func() {
		fmt.Fprintf(os.Stderr, "usage: sbxrun [--sandbox ID] [--timeout S] [-c CMD] [-- CMD ...] < script\n"+
			"  exit: the command's code; 124 timed out or killed; 125 sandbox failure\n")
		flag.PrintDefaults()
	}
	flag.Parse()

	if *sandbox == "" {
		fmt.Fprintln(os.Stderr, "sbxrun: --sandbox is required")
		os.Exit(2)
	}
	command := *cmd
	if flag.NArg() > 0 {
		command = strings.Join(flag.Args(), " ")
	}

	stdin, _ := io.ReadAll(os.Stdin)

	// --control-plane-url now only forwards to mocactl (via MOCACTL_SBX_URL);
	// sbxrun itself no longer talks to the control plane.
	if *cpURL != "" {
		os.Setenv("MOCACTL_SBX_URL", *cpURL)
	}

	t0 := time.Now()
	tok, err := getSandboxToken(*sandbox, false)
	endpoint := os.Getenv("SBXRUN_ADDR")
	if endpoint == "" && tok != nil {
		endpoint = tok.ExecEndpoint
	}
	var client *http.Client
	if err == nil {
		client, err = newExecClient(tok.ExecCa, endpoint)
	}
	var code int32
	if err == nil {
		var attemptErr error
		// NB: ExecRequest.sandbox_id is the WORKER/template id (wrk, e.g.
		// density_default_256); the sbx-... id is the workspace_key (wsk).
		code, attemptErr = oneExec(client, endpoint, tok.Token, tok.Wrk,
			tok.Wsk, command, stdin, uint32(*timeout), os.Stdout, os.Stderr)
		// one retry with a forced fresh token on auth refusal (mocactl's rule)
		if attemptErr != nil && client != nil && strings.Contains(strings.ToLower(attemptErr.Error()), "unauthenticated") {
			if tok2, e := getSandboxToken(*sandbox, true); e == nil {
				code, attemptErr = oneExec(client, endpoint, tok2.Token, tok2.Wrk,
					tok2.Wsk, command, stdin, uint32(*timeout), os.Stdout, os.Stderr)
			}
		}
		err = attemptErr
	}
	if err != nil {
		// the worker's timeout arrives as ExecError "timeout:<n>": mocactl
		// reports 124 for it (a timeout is the command's outcome)
		if strings.HasPrefix(err.Error(), "ExecError: timeout:") {
			fmt.Fprintf(os.Stderr, "sbxrun: %v\n", err)
			os.Exit(exitTimeout)
		}
		fmt.Fprintf(os.Stderr, "sbxrun: %v\n", err)
		os.Exit(exitMachinery)
	}
	if *verbose {
		fmt.Fprintf(os.Stderr, "sbxrun: %s total\n", time.Since(t0).Round(time.Millisecond))
	}
	if code == -999 {
		os.Exit(exitMachinery)
	}
	if code < 0 {
		// killed by a signal: mocactl reports 124 for its timeouts
		os.Exit(exitTimeout)
	}
	os.Exit(int(code))
}

// stringFlag helper (flag.String with shorthand is awkward for "-c")
func stringFlag(name, def, usage string) *string {
	return flag.String(name, def, usage)
}

var _ = strconv.Itoa
