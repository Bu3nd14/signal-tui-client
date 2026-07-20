"""
Script to link a new Signal device as a secondary client.
Generates a QR code to scan with the Signal app on your smartphone.
The signal-cli process stays running waiting for the scan.
"""

import subprocess
import sys
import re
import signal
from pathlib import Path

import qrcode


def find_signal_cli() -> Path:
    """Find the signal-cli executable in the ./bin/ directory of the project."""
    bin_dir = Path(__file__).parent / "bin"
    # Look for pattern: bin/signal-cli-*/bin/signal-cli
    for d in bin_dir.iterdir():
        if d.is_dir() and d.name.startswith("signal-cli-"):
            exe = d / "bin" / "signal-cli"
            if exe.exists() and exe.stat().st_mode & 0o111:
                return exe
    raise FileNotFoundError(
        "signal-cli not found in ./bin/. Run the download step first."
    )


def print_qr_code(link: str) -> None:
    """
    Generate and print the QR code in the terminal using the qrcode library.
    """
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=2,
        border=2,
    )
    qr.add_data(link)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


def main():
    # Find signal-cli
    try:
        signal_cli_path = find_signal_cli()
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print("=" * 60)
    print("  🔗 Link new Signal device")
    print("  📱 Scan the QR code with the Signal app on your phone")
    print("=" * 60)
    print()
    print(f"✅ signal-cli found: {signal_cli_path}")
    print(f"⏳ Starting link command...")
    print()

    # Start signal-cli link in a separate process (non-blocking)
    proc = subprocess.Popen(
        [str(signal_cli_path), "link", "-n", "Mac-TUI-Client"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
    )

    link_found = False

    # Read output line by line in real time
    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip()
        print(line)

        if not link_found:
            # Look for the link URL (format: sgnl://linkdevice?... or signal://link/...)
            match = re.search(r"((?:sgnl|signal)://link[^\s]*)", line)
            if match:
                link = match.group(1)
                link_found = True
                print()
                print("=" * 60)
                print("  📸 SCAN THE QR CODE WITH THE SIGNAL APP:")
                print()
                print_qr_code(link)
                print()
                print("  ⏳ Waiting for scan from phone...")
                print("  (press Ctrl+C to cancel)")
                print("=" * 60)
                print()

    # When signal-cli finishes, wait for the exit code
    proc.wait()

    if proc.returncode == 0:
        print()
        print("✅ Device linked successfully!")
    else:
        print()
        print(f"❌ signal-cli exited with error (code: {proc.returncode})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("⏹ Operation cancelled by user.")
        sys.exit(0)
