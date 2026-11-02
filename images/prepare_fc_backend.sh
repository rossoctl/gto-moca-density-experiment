#!/bin/bash
# prepare_fc_backend.sh -- one-time prep for `--backend fc` (replay/fc.py):
# builds the guest client that speaks the guest agent's framed protocol over
# the vsock uds, and checks the host has what the backend needs.
#
# The guest client is GENERATED inside the pinned moca checkout's module tree
# and built with its go — the exact mechanism deploy/microvm/build-snapshot.sh
# uses (write_guest_client): the file imports internal/guestagent, which Go's
# internal-package rule only allows from inside the module, so the wire format
# keeps exactly one implementation, in the moca repo, pinned by MOCA_COMMIT
# like every other moca-built artifact this experiment uses.
#
# Usage: MOCADIR=~/repo/rossoctl/moca ./prepare_fc_backend.sh
set -euo pipefail
cd "$(dirname "$0")"
MOCADIR=${MOCADIR:-$HOME/repo/rossoctl/moca}
# absolute before anything below cd's around (the build runs inside the
# moca checkout, so a relative OUT would resolve against the wrong tree)
OUT=${OUT:-"$(cd "$(dirname "$0")/.." && pwd)/replay/fc-guest-client"}
mkdir -p "$(dirname "$OUT")"

fail() { echo "prepare_fc_backend.sh: $*" >&2; exit 1; }

[ -x /usr/local/bin/firecracker ] || fail "no /usr/local/bin/firecracker (the moca install provides it)"
[ -x /usr/local/bin/jailer ] || fail "no /usr/local/bin/jailer"
[ -d "${FC_SNAPSHOTS:-/srv/sbx/snapshots}" ] || fail "no ${FC_SNAPSHOTS:-/srv/sbx/snapshots} (golden snapshots; the moca install provides them)"
command -v go >/dev/null || fail "no go toolchain"
[ -d "$MOCADIR/remote-worker" ] || fail "no moca checkout at $MOCADIR (MOCADIR=...)"
[ -w "$MOCADIR/remote-worker" ] || fail "$MOCADIR/remote-worker not writable (guest_client.go must be generated inside the module tree)"

# The base for the backend's jails and workspaces must sit on the SAME
# filesystem as the snapshots (hardlinks), and outside moca's /srv/sbx.
FC_BASE=${FC_BASE:-/srv/sbx-purefc}
if [ "$(df --output=target "${FC_SNAPSHOTS:-/srv/sbx/snapshots}" | tail -1)" != "$(df --output=target "$FC_BASE" 2>/dev/null | tail -1)" ]; then
  if [ ! -d "$FC_BASE" ]; then
    echo "note: $FC_BASE does not exist yet; it must be created on the same filesystem as the snapshots (hardlinks)" >&2
  else
    fail "$FC_BASE is on a different filesystem than the snapshots; hardlinks cannot cross"
  fi
fi

tmp_pkg="$MOCADIR/remote-worker/.fc-backend-tmp-$$"
cleanup() { rm -rf "$tmp_pkg"; }
trap cleanup EXIT
mkdir -p "$tmp_pkg"

# guest_client.go: verbatim from deploy/microvm/build-snapshot.sh's
# write_guest_client (same pinned checkout; keep the copies identical or the
# protocol implementation forks).
cat > "$tmp_pkg/guest_client.go" <<'GOEOF'
// Command guest_client is a build-time-only helper: it speaks the same framed
// protocol internal/vmpool/guestconn.go speaks at serve time, but from a
// throwaway process instead of the pool, because build-snapshot.sh has no pool.
//
// Firecracker's and Cloud Hypervisor's Unix-socket vsock backends both proxy a
// host connection into the guest's listener on a fixed port via a one-line
// handshake: the host writes "CONNECT <port>\n" on the VMM-created Unix socket and,
// once the guest has accept()ed, the socket becomes a raw duplex stream to the
// guest side. That handshake is VMM-and-version-specific; verify it against the
// VMM's current vsock documentation when this runs for real (Task 12's KVM gate),
// since it cannot be exercised without a hypervisor.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	ga "github.com/rossoctl/moca/remote-worker/internal/guestagent"
)

func main() {
	uds := flag.String("uds", "", "path to the VMM's vsock unix socket")
	port := flag.Uint("port", 1024, "guest vsock port the agent listens on")
	command := flag.String("command", "", "command to run in the guest; empty means probe-only")
	timeoutS := flag.Uint("timeout-s", 30, "guest-side command timeout")
	probeOnly := flag.Bool("probe-only", false, "just prove the guest is accepting; run nothing")
	dialTimeout := flag.Duration("dial-timeout", 10*time.Second, "how long to wait for the CONNECT handshake")
	flag.Parse()

	if *uds == "" {
		fmt.Fprintln(os.Stderr, "guest_client: -uds is required")
		os.Exit(2)
	}

	conn, err := dialGuest(*uds, uint32(*port), *dialTimeout)
	if err != nil {
		fmt.Fprintf(os.Stderr, "guest_client: %v\n", err)
		os.Exit(1)
	}
	defer conn.Close()

	if *probeOnly {
		return
	}

	req := ga.Request{Command: *command, TimeoutS: uint32(*timeoutS), CapBytes: ga.MaxFrame, HostUnixNanos: time.Now().UnixNano()}
	if err := ga.WriteJSON(conn, ga.KindRequest, req); err != nil {
		fmt.Fprintf(os.Stderr, "guest_client: send request: %v\n", err)
		os.Exit(1)
	}
	if err := ga.WriteFrame(conn, ga.KindStdinEOF, nil); err != nil {
		fmt.Fprintf(os.Stderr, "guest_client: send stdin-eof: %v\n", err)
		os.Exit(1)
	}

	for {
		kind, payload, err := ga.ReadFrame(conn)
		if err != nil {
			fmt.Fprintf(os.Stderr, "guest_client: read: %v\n", err)
			os.Exit(1)
		}
		switch kind {
		case ga.KindStdout:
			os.Stdout.Write(payload)
		case ga.KindStderr:
			os.Stderr.Write(payload)
		case ga.KindEnd:
			var e ga.End
			if err := json.Unmarshal(payload, &e); err != nil {
				fmt.Fprintf(os.Stderr, "guest_client: undecodable End: %v\n", err)
				os.Exit(1)
			}
			os.Exit(int(e.ExitCode))
		case ga.KindError:
			fmt.Fprintf(os.Stderr, "guest_client: guest error: %s\n", payload)
			os.Exit(1)
		}
	}
}

// dialGuest performs the VMM's vsock Unix-socket CONNECT handshake and returns the
// resulting stream. See the package comment for what is and is not verified here.
func dialGuest(uds string, port uint32, timeout time.Duration) (net.Conn, error) {
	d := net.Dialer{Timeout: timeout}
	conn, err := d.Dial("unix", uds)
	if err != nil {
		return nil, fmt.Errorf("dial %s: %w", uds, err)
	}
	if _, err := fmt.Fprintf(conn, "CONNECT %d\n", port); err != nil {
		conn.Close()
		return nil, fmt.Errorf("send CONNECT: %w", err)
	}
	_ = conn.SetReadDeadline(time.Now().Add(timeout))
	line, err := readLine(conn)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("read CONNECT ack: %w", err)
	}
	if !strings.HasPrefix(line, "OK") {
		conn.Close()
		return nil, fmt.Errorf("CONNECT %d refused: %q", port, line)
	}
	_ = conn.SetReadDeadline(time.Time{})
	return conn, nil
}

func readLine(conn net.Conn) (string, error) {
	buf := make([]byte, 0, 64)
	one := make([]byte, 1)
	for {
		if _, err := conn.Read(one); err != nil {
			return "", err
		}
		if one[0] == '\n' {
			return string(buf), nil
		}
		buf = append(buf, one[0])
	}
}
GOEOF

(cd "$MOCADIR/remote-worker" && go build -o "$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")" "$tmp_pkg/guest_client.go")
echo "built: $OUT"
echo "moca commit used (record with results): $(git -C "$MOCADIR" rev-parse HEAD)"
