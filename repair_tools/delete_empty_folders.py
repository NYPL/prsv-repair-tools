"""
Script to delete empty folders within package directories, ignoring protected system names.
"""
import argparse
from repair_tools.utils.cli import extant_dir, list_of_paths, ExtendUnique as StoreListAction
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(message)s')

class ExtendUnique(argparse.Action):
    """Custom argparse action to collect unique values in a set."""
    def __call__(self, parser, namespace, values, option_string=None):
        items = getattr(namespace, self.dest, None)

        if items is None:
            items = set(values)
        elif isinstance(items, set):
            items = items.union(values)

        setattr(namespace, self.dest, items)

def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Delete empty folders within a directory of packages, ignoring specific protected names.")

    parser.add_argument(
            "--directory",
            type=list_of_paths,
            dest="directories", 
            action=ExtendUnique,
            help="path to a directory of packages",
            required=True
        )
    return parser.parse_args()
    


def manage_empty_folders(directories: set[Path]):
    """Find and delete empty folders within the specified directories, excluding protected names."""
    empty_folders_to_delete = []
    protected_names = {'servicecopies', 'preservationmasters', 'ServiceCopies', 'PreservationMasters', 'Service Copies', 'Preservation Masters'}

    logging.info("Scanning for empty directories.")
    for package_path in directories:
        for folder in sorted(package_path.rglob('*'), key=lambda p: len(p.parts), reverse=True):
            if folder.is_dir():
                normalized_name = folder.name.lower().replace(" ", "").replace("_", "").replace("-", "")
                
                if normalized_name in protected_names:
                    continue
                
                is_empty = True
                for child in folder.iterdir():
                    if child not in empty_folders_to_delete:
                        is_empty = False
                        break
                
                if is_empty:
                    empty_folders_to_delete.append(folder)

    if not empty_folders_to_delete:
        logging.info("No empty folders found.")
        return

    logging.info(f"\nFound {len(empty_folders_to_delete)} empty folder(s):")
    # sort back to top-down or alphabetical for nicer display
    for f in sorted(empty_folders_to_delete):
        logging.info(f" - {f}")

    confirm = input("Do you want to delete these folders? (y/n): ").strip().lower()

    if confirm == 'y':
        count = 0
        for f in empty_folders_to_delete:
            try:
                f.rmdir()
                logging.info(f"Deleted: {f.name}")
                count += 1
            except OSError as e:
                logging.error(f"Error deleting {f.name}: {e}")
        logging.info(f"{count} folders deleted.")
    else:
        logging.info("No folders were deleted.")

def main():
    """Main entry point for the script."""
    args = parse_args()
    
    if args.directories:
        manage_empty_folders(args.directories)
    else:
        logging.info("No valid directories provided.")

if __name__ == "__main__":
    main()

