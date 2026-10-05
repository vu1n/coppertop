import argparse
from pathlib import Path
from .chords import LocalStore
from .ingest import ingest
from .lerobot import export


def main():
    parser = argparse.ArgumentParser(description='Local Chords milestone')
    parser.add_argument('--store', type=Path, default=Path('.local/store'))
    commands = parser.add_subparsers(dest='command', required=True)
    importer = commands.add_parser('import')
    importer.add_argument('source', type=Path)
    exporter = commands.add_parser('export')
    exporter.add_argument('release')
    exporter.add_argument('destination', type=Path)
    args = parser.parse_args()
    store = LocalStore(args.store)
    if args.command == 'import':
        print(ingest(args.source, store))
    else:
        export(store, args.release, args.destination)


if __name__ == '__main__':
    main()
