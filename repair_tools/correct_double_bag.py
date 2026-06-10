import argparse
import logging
import subprocess
import shutil
from pathlib import Path
import repair_tools.utils.cli as cli

logging.basicConfig(level=logging.INFO)

def parse_args() -> argparse.Namespace:
    parser = cli.Parser()
    parser.add_packagedirectory()

def edit_manifest(directory:Path):
    for manifest in directory.rglob('manifest-md5.txt'):
        subprocess.run(["sed", "-i", "", 's|data/data|data|', manifest.absolute()])

def rm_tagmanifest(directory: Path):
    for tag_manifest in directory.rglob('tagmanifest-md5.txt'):
        tag_manifest.unlink()

def correct_dbl_bag(directory: Path):
    for dir in directory.rglob('*'):
        if "data" in dir.parents[0].name and "data" in dir.parents[1].name:
            parent_dir = dir.parents[1]
            shutil.move(dir, parent_dir)

def main():
    # args = parse_args()
    directory = Path("/Volumes/lpasync/prsv_prod_ingest/double-bag")

    edit_manifest(directory)
    rm_tagmanifest(directory)
    correct_dbl_bag(directory)


if __name__ == "__main__":
    main()