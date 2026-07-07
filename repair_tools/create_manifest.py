from pathlib import Path
import argparse
import concurrent.futures
import sys
import hashlib

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bagpath",
        type=Path,
        nargs="*",
        required=True,
        help="Path to the base of the bag where the manifest will be created/verified"
    )
    parser.add_argument(
        "--md5",
        action='store_true',
        help="Option to generate md5 checksums for missing files (default is placeholder zeros)"
    )
    parser.add_argument(
        "--verify",
        action='store_true',
        help="Verify the integrity of an existing manifest (recalculates checksums)"
    )
    return parser.parse_args()

def _calculate_md5(filepath: Path) -> str:
    print(f"      >Working on: {filepath.name}...")
    with open(filepath, 'rb') as f:
        return hashlib.file_digest(f, 'md5').hexdigest()

def verify_manifest(bag_path: Path):
    if bag_path.name == "data":
        bag_root = bag_path.parent
    else:
        bag_root = bag_path

    manifest_file = bag_root / "manifest-md5.txt"

    if not manifest_file.exists():
        print(f"  ERROR: No manifest found at '{manifest_file}' to verify.", file=sys.stderr)
        return

    print(f"  Verifying manifest at '{manifest_file}'...")
    
    manifest_entries = {}
    try:
        with open(manifest_file, 'r', encoding='utf-8') as m5f:
            for line in m5f:
                parts = line.strip().split("  ")
                if len(parts) == 2:
                    manifest_entries[parts[1]] = parts[0]
    except Exception as e:
        print(f"    ERROR: Could not read manifest: {e}", file=sys.stderr)
        return

    if not manifest_entries:
        print("    WARNING: Manifest is empty.", file=sys.stderr)
        return

    print(f"    Found {len(manifest_entries)} entries...")
    
    errors = 0
    success = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future_to_path = {}
        for rel_path_str, expected_hash in manifest_entries.items():
            full_path = bag_root / rel_path_str
            if not full_path.exists():
                print(f"    [MISSING] File listed in manifest is missing: {rel_path_str}", file=sys.stderr)
                errors += 1
                continue
            future_to_path[executor.submit(_calculate_md5, full_path)] = (rel_path_str, expected_hash)

        try:
            for future in concurrent.futures.as_completed(future_to_path):
                rel_path_str, expected_hash = future_to_path[future]
                try:
                    calculated_hash = future.result()
                    if expected_hash == "00000000000000000000000000000000":
                        print(f"    [SKIPPED] Placeholder MD5 ignored for: {rel_path_str}")
                        success += 1
                    elif calculated_hash != expected_hash:
                        print(f"    [CORRUPTED] Hash mismatch for: {rel_path_str}", file=sys.stderr)
                        print(f"       Expected: {expected_hash}", file=sys.stderr)
                        print(f"       Calculated: {calculated_hash}", file=sys.stderr)
                        errors += 1
                    else:
                        success += 1
                except (IOError, OSError) as e:
                    if not bag_root.exists():
                        print(f"\n    CRITICAL: Network drive disconnected. ({bag_root})", file=sys.stderr)
                        break
                    else:
                        print(f"    [ERROR] Could not read {rel_path_str}: {e}", file=sys.stderr)
                        errors += 1
        except KeyboardInterrupt:
            print("\n    WARNING: Verification interrupted by user.")

    print("-" * 40)
    print(f"  Verification Complete for {bag_root.name}:")
    print(f"  Successfully Verified: {success}")
    print(f"  Errors/Missing: {errors}")
    print("-" * 40)

def create_manifest(bag_path: Path, manifest_paths=None, generate_md5: bool=False):
    if bag_path.name == "data":
        bag_root = bag_path.parent
        data_dir = bag_path
    else:
        bag_root = bag_path
        data_dir = bag_path / "data" 

    manifest_file = bag_root / "manifest-md5.txt"
    DUMMY_MD5 = "00000000000000000000000000000000"

    print(f"  Processing bag at '{bag_root}'")

    existing_entries = []
    already_hashed_paths = set()
    
    if manifest_file.exists():
        try:
            with open(manifest_file, 'r', encoding='utf-8') as m5f:
                existing_entries = m5f.readlines()
                for line in existing_entries:
                    parts = line.strip().split("  ") 
                    if len(parts) == 2:
                        already_hashed_paths.add(parts[1])
        except Exception as e:
            print(f"    ERROR: Could not read existing manifest: {e}", file=sys.stderr)

    files_to_process = []
    
    if manifest_paths:
        print(f"    Using provided list of {len(manifest_paths)} files.")
        raw_files = manifest_paths
    else:
        print(f"    Discovering files in '{data_dir}'...")
        if not data_dir.is_dir():
            print(f"    WARNING: Data directory '{data_dir}' does not exist. Manifest will be empty.", file=sys.stderr)
            raw_files = []
        else:
            raw_files = [f for f in data_dir.rglob('*') if (f.is_file() and not f.name.startswith("."))]
        
        if not raw_files and not existing_entries:
            print("    WARNING: No files found to add to manifest.", file=sys.stderr)

    # 3. Filter out files that are already in the manifest
    for f in raw_files:
        try:
            relative_path_str = str(f.relative_to(bag_root)).replace('\\', '/')
            if relative_path_str not in already_hashed_paths:
                files_to_process.append(f)
        except ValueError:
            continue

    if not files_to_process:
        print(f"    All {len(already_hashed_paths)} files are already hashed in the manifest. Skipping.")
        return

    try:
        # Sort remaining files by size (smallest to largest)
        try:
            files_to_process.sort(key=lambda f: f.stat().st_size)
        except Exception as e:
            print(f"    WARNING: Could not sort files by size: {e}", file=sys.stderr)

        print(f"    Total MISSING files to add to manifest: {len(files_to_process)}")
        new_manifest_entries = []

        if generate_md5:
            print(f"    Hashing {len(files_to_process)} missing files (smallest files first):")

            # local storage, more workers / HDDs = 1, SSD = 2-3, mounted storage must be set to 1!
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future_to_path = {executor.submit(_calculate_md5, path): path for path in files_to_process}

                try:
                    for future in concurrent.futures.as_completed(future_to_path):
                        filepath = future_to_path[future]
                        try:
                            md5_hash = future.result()
                            if md5_hash:
                                relative_path_str = str(filepath.relative_to(bag_root)).replace('\\', '/')
                                new_manifest_entries.append(f"{md5_hash}  {relative_path_str}\n")
                        
                        except (IOError, OSError) as e:
                            # Emergency unmount check
                            if not bag_root.exists():
                                print(f"\n    CRITICAL: Network drive disconnected. ({bag_root})", file=sys.stderr)
                                print("    Halting checksum generation and initiating emergency save...", file=sys.stderr)
                                break 
                            else:
                                print(f"    ERROR: Could not read file {filepath}: {e}", file=sys.stderr)
                        except Exception as e:
                            print(f"    ERROR: Exception hashing {filepath}: {e}", file=sys.stderr)
                
                except KeyboardInterrupt:
                    print("\n    WARNING: Process interrupted manually, saving partial manifest...")
                except Exception as e:
                    print(f"\n    ERROR: Hashing abruptly stopped: {e}. Saving partial manifest...", file=sys.stderr)

        else:
            print(f"    Generating placeholder entries for {len(files_to_process)} missing files...")
            for filepath in files_to_process:
                try:
                    relative_path_str = str(filepath.relative_to(bag_root)).replace('\\', '/')
                    new_manifest_entries.append(f"{DUMMY_MD5}  {relative_path_str}\n")
                except ValueError:
                    print(f"    WARNING: File '{filepath}' is not inside the bag root '{bag_root}'. Skipping.", file=sys.stderr)
                    continue
        
        all_entries = existing_entries + new_manifest_entries
        all_entries.sort(key=lambda x: x.split("  ")[1] if "  " in x else x)

        if all_entries:
            if not bag_root.exists():
                manifest_file = Path.cwd() / f"RESCUED_manifest_{bag_root.name}.txt"
                print(f"\n    WARNING: Original bag path is inaccessible.", file=sys.stderr)
                print(f"    Saving progress locally to: {manifest_file}", file=sys.stderr)
            
            try:
                with open(manifest_file, 'w', encoding='utf-8') as m5f:
                    m5f.writelines(all_entries)
                    print(f"    Manifest successfully updated. Total entries: {len(all_entries)}.")
            except Exception as e:
                print(f"    CRITICAL ERROR: Could not save manifest. {e}", file=sys.stderr)
        else:
            print("    WARNING: No entries to write. Manifest was not created.", file=sys.stderr)
    
    except Exception as e:
        print(f"    ERROR: Critical failure during manifest creation: {e}", file=sys.stderr)

def main():
    args = parse_args()

    for bag_path in sorted(args.bagpath):
        if args.verify:
            verify_manifest(bag_path)
        elif args.md5:
            create_manifest(bag_path, generate_md5=True)
        else:
            create_manifest(bag_path)

if __name__ == "__main__":
    main()