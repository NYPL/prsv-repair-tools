from pathlib import Path
import argparse
import subprocess
import sys
import hashlib

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bagpath",
        type=Path,
        required=True,
        help="Path to the base of the bag where the manifest will be created"
    )
    parser.add_argument(
        "--md5",
        action='store_true',
        help="Option to generate md5 checksums for manifest (default is placeholder zeros)"
    )
    return parser.parse_args()

def _calculate_md5(filepath: Path, block_size: int = 65536) -> str:
    md5 = hashlib.md5()
    try:
        with open(filepath, 'rb') as f:
            while True:
                data = f.read(block_size)
                if not data:
                    break
                md5.update(data)
    except (IOError, OSError) as e:
        print(f"    ERROR: Could not read file {filepath} for hashing: {e}", file=sys.stderr)
        return None
    return md5.hexdigest()

def create_manifest(bag_path: Path, manifest_paths = None, generate_md5: bool = False):
    if bag_path.name == "data":
        bag_root = bag_path.parent
        data_dir = bag_path
    else:
        bag_root = bag_path
        data_dir = bag_path / "data" 

    manifest_file = bag_root / "manifest-md5.txt"
    DUMMY_MD5 = "00000000000000000000000000000000"

    if manifest_file.exists():
        print(f"  WARNING: Manifest file already exists at '{manifest_file}'. Skipping.", file=sys.stderr)
        return

    print(f"  Creating manifest at '{manifest_file}'")

    files_to_process = []
    
    if manifest_paths:
        print(f"    Using provided list of {len(manifest_paths)} files.")
        files_to_process = manifest_paths
    else:
        print(f"    Discovering files in '{data_dir}'...")
        if not data_dir.is_dir():
            print(f"    WARNING: Data directory '{data_dir}' does not exist. Manifest will be empty.", file=sys.stderr)
        else:
            files_to_process = [f for f in data_dir.rglob('*') if f.is_file()]
        
        if not files_to_process:
            print("    WARNING: No files found to add to manifest.", file=sys.stderr)

    file_count = 0
    try:
        with open(manifest_file, 'w', encoding='utf-8') as m5f:
            print(f"    Total files to add to manifest: {len(files_to_process)}")
            for filepath in files_to_process:
                if not filepath.is_file():
                    print(f"    WARNING: Item '{filepath}' does not exist or is not a file. Skipping.", file=sys.stderr)
                    continue
                try:
                    relative_path_str = str(filepath.relative_to(bag_root)).replace('\\', '/')
                except ValueError:
                    print(f"    WARNING: File '{filepath}' is not inside the bag root '{bag_root}'. Skipping.", file=sys.stderr)
                    continue

                if generate_md5:
                    print(f"    Adding to manifest: {relative_path_str}")
                    md5_hash = _calculate_md5(filepath)
                    if md5_hash is None:
                        continue 
                else:
                    md5_hash = DUMMY_MD5

                m5f.write(f"{md5_hash}  {relative_path_str}\n")
                file_count += 1
        print(f"    Manifest created successfully with {file_count} entries.")
    
    except Exception as e:
        print(f"    ERROR: Could not create manifest file at '{manifest_file}': {e}", file=sys.stderr)
        if manifest_file.exists():
            manifest_file.unlink()

def main():
    args = parse_args()
    bag_path = args.bagpath
    if args.md5:
        create_manifest(bag_path, generate_md5=True)
    else:
        create_manifest(bag_path)

if __name__ == "__main__":
    main()