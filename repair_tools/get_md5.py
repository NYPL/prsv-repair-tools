import hashlib
import logging
import sys
from pathlib import Path
from repair_tools.cli import Parser

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger(__name__)

def calculate_md5(file_path: Path, block_size: int = 65536) -> str:
    """Calculates the MD5 checksum of a file."""
    md5 = hashlib.md5()
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(block_size), b''):
                md5.update(chunk)
    except (IOError, OSError) as e:
        logger.error(f"Could not read file {file_path} for hashing: {e}")
        return None
    return md5.hexdigest()

def find_files(pkg_path: Path, folders: list[str]) -> list[Path]:
    """Finds all files in the specified subfolders of the package data directory."""
    files = []
    for folder in folders:
        target_dir = pkg_path / 'data' / folder
        if target_dir.exists() and target_dir.is_dir():
            files.extend([f for f in target_dir.rglob("*") if f.is_file() and not f.name.startswith("._")])
        else:
            logger.warning(f"Directory not found: {target_dir}")
    return files

def main():
    parser = Parser(description='Get MD5 checksums for files in PreservationMasters and/or ServiceCopies subfolders.')
    parser.add_package()
    parser.add_packagedirectory()
    
    parser.add_argument('--pm', action='store_true', help='Include PreservationMasters subfolder')
    parser.add_argument('--sc', action='store_true', help='Include ServiceCopies subfolder')
    parser.add_argument('--all', action='store_true', help='Include both PreservationMasters and ServiceCopies subfolders')
    
    args = parser.parse_args()
    
    if not (args.pm or args.sc or args.all):
        parser.error("At least one flag (--pm, --sc, --all) must be provided.")

    folders = []
    if args.all or args.pm:
        folders.append('PreservationMasters')
    if args.all or args.sc:
        folders.append('ServiceCopies')
        
    if not getattr(args, 'packages', None):
        logger.warning("No package or directory provided. Use --package or --directory.")
        return

    for pkg in args.packages:
        logger.info(f"Scanning package: {pkg}")
        files = find_files(pkg, folders)
        
        if not files:
            logger.info(f"No files found in {folders} for package {pkg.name}")
            continue

        for f in sorted(files):
            checksum = calculate_md5(f)
            if checksum:
                try:
                    relative_path = f.relative_to(pkg)
                except ValueError:
                    relative_path = f
                
                print(f"{checksum}  {relative_path}")

if __name__ == '__main__':
    main()
