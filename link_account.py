"""
Script per associare un nuovo dispositivo Signal come client secondario.
Genera un QR code da scansionare con l'app Signal dello smartphone.
Il processo signal-cli rimane in esecuzione in attesa della scansione.
"""

import subprocess
import sys
import re
import signal
from pathlib import Path

import qrcode


def find_signal_cli() -> Path:
    """Cerca l'eseguibile signal-cli nella directory ./bin/ del progetto."""
    bin_dir = Path(__file__).parent / "bin"
    # Cerca pattern: bin/signal-cli-*/bin/signal-cli
    for d in bin_dir.iterdir():
        if d.is_dir() and d.name.startswith("signal-cli-"):
            exe = d / "bin" / "signal-cli"
            if exe.exists() and exe.stat().st_mode & 0o111:
                return exe
    raise FileNotFoundError(
        "signal-cli non trovato in ./bin/. Esegui prima lo scaricamento."
    )


def print_qr_code(link: str) -> None:
    """
    Genera e stampa il QR code nel terminale usando la libreria qrcode.
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
    # Trova signal-cli
    try:
        signal_cli_path = find_signal_cli()
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    print("=" * 60)
    print("  🔗 Associazione nuovo dispositivo Signal")
    print("  📱 Scansiona il QR code con l'app Signal del tuo smartphone")
    print("=" * 60)
    print()
    print(f"✅ signal-cli trovato: {signal_cli_path}")
    print(f"⏳ Avvio del comando di link...")
    print()

    # Avvia signal-cli link in un processo separato (non bloccante)
    proc = subprocess.Popen(
        [str(signal_cli_path), "link", "-n", "Mac-TUI-Client"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
    )

    link_found = False

    # Legge l'output riga per riga in tempo reale
    for line in iter(proc.stdout.readline, ""):
        line = line.rstrip()
        print(line)

        if not link_found:
            # Cerca il link di associazione (formato sgnl://linkdevice?... o signal://link/...)
            match = re.search(r"((?:sgnl|signal)://link[^\s]*)", line)
            if match:
                link = match.group(1)
                link_found = True
                print()
                print("=" * 60)
                print("  📸 INQUADRA IL QR CODE CON L'APP SIGNAL:")
                print()
                print_qr_code(link)
                print()
                print("  ⏳ In attesa della scansione dal telefono...")
                print("  (premi Ctrl+C per annullare)")
                print("=" * 60)
                print()

    # Quando signal-cli termina, attendi il codice di uscita
    proc.wait()

    if proc.returncode == 0:
        print()
        print("✅ Dispositivo associato con successo!")
    else:
        print()
        print(f"❌ signal-cli terminato con errore (codice: {proc.returncode})")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        print("⏹ Operazione annullata dall'utente.")
        sys.exit(0)
