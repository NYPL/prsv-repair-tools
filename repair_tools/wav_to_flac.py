import argparse
import subprocess
import logging
import re
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from repair_tools.cli import Parser

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

FLAC_COMMAND_BASE = ['flac', '--keep-foreign-metadata', '--best', '--preserve-modtime', '--verify']

def parse_args() -> argparse.Namespace:
    parser = Parser(description='Compress .wav PM files to .flac files within AMI packages.')
    # group = parser.add_mutually_exclusive_group(required=True)
    parser.add_package()
    parser.add_packagedirectory()
    
    return parser.parse_args()

def transcode_single_file(input_file: Path, output_file: Path) -> bool:
    """Transcode a single file from WAV to FLAC (with subprocess.run)."""
    flac_command = FLAC_COMMAND_BASE + [str(input_file), '-o', str(output_file)]
    try:
        result = subprocess.run(flac_command, capture_output=True, text=True, check=True)
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"FLAC transcoding failed for {input_file.name}: {e.stderr}")
        return False

def find_wav_files(pkg_path: Path) -> list[Path]:
    """Finds all WAV files in PreservationMasters and EditMasters for a given package."""
    wav_files = []
    
    pm_dir = pkg_path / 'data' / 'PreservationMasters'
    em_dir = pkg_path / 'data' / 'EditMasters'
    
    for main_dir in [pm_dir, em_dir]:
        if main_dir.exists() and main_dir.is_dir():
            wav_files.extend([f for f in main_dir.rglob("*.wav") if not f.name.startswith("._")])
        else:
            logger.warning(f"{main_dir.name} directory not found in {pkg_path.name}")

    return wav_files

def process_file_worker(wav_file: Path) -> tuple[Path, bool]:
    output_file = wav_file.with_suffix(".flac")
    
    if output_file.exists():
        logger.info(f"FLAC already exists, skipping: {output_file.name}")
        return output_file, True
        
    logger.info(f"Transcoding {wav_file.name} to {output_file.name}")
    success = transcode_single_file(wav_file, output_file)
    if success:
        logger.info(f"Successfully transcoded: {output_file.name}")
        wav_file.unlink()
        logger.info(f"Deleted: {wav_file.name}")
    else:
        logger.error(f"Failed to transcode: {wav_file.name}")
    return output_file, success

def main() -> None:
    args = parse_args()
    
    if getattr(args, 'packages', None) is None:
        logger.warning("No package or directory provided. Please use --package or --directory.")
        return

    dir_pattern = re.compile(r"^\d{6}$")
    valid_packages = [pkg for pkg in args.packages if dir_pattern.match(pkg.name)]

    if not valid_packages:
        logger.warning("No valid package directories (matching 6 digits) found to process.")
        return

    logger.info(f"Found {len(valid_packages)} valid package(s) to process. Scanning for WAV files...")

    all_wav_files = []
    for pkg in valid_packages:
        all_wav_files.extend(find_wav_files(pkg))
        
    if not all_wav_files:
        logger.info("No WAV PM files found in any of the provided packages.")
        return

    workers = os.cpu_count() or 4
    logger.info(f"Initiating batch compression for {len(all_wav_files)} WAV files using {workers} workers.")

    success_count = 0
    failure_count = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Submit all tasks
        futures = {executor.submit(process_file_worker, wav): wav for wav in sorted(all_wav_files)}
        
        for future in as_completed(futures):
            wav_file = futures[future]
            try:
                output_file, success = future.result()
                if success:
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as e:
                logger.error(f"Exception raised while processing {wav_file.name}: {e}")
                failure_count += 1

    logger.info(f"Processing completed: {success_count} successful, {failure_count} failed.")

if __name__ == '__main__':
    main()
