from pathlib import Path
import argparse

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bagpath",
        type=Path,
        required=True,
        help="Path to the base of the bag where thebag structure will be created"
    )
    return parser.parse_args()

def create_dir_structure(base_path: Path, ami_id: str):
    bag_path = base_path / ami_id / "data" 
    pm_path = bag_path / "PreservationMasters"
    sc_path = bag_path / "ServiceCopies"
    if not pm_path.exists() or not sc_path.exists():
        print(f"Creating bag structure for '{ami_id}' at: '{bag_path}'")
        pm_path.mkdir(parents=True, exist_ok=True)
        sc_path.mkdir(parents=True, exist_ok=True)
    else:
        print(f"Bag structure for '{ami_id}' already exists at: '{bag_path}'")
    return bag_path, pm_path, sc_path

def main():
    args = parse_args()

    bag_path, pm_path, sc_path = create_dir_structure(args.bagpath, args.bagpath.name)
    
if __name__ == "__main__":
    main()
    