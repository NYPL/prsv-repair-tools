import argparse
import subprocess
import logging
import re
import os
import sys
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm
from repair_tools.utils.cli import Parser

# logging handler to play nice with tqdm
class TqdmLoggingHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            tqdm.write(msg)
            self.flush()
        except Exception:
            self.handleError(record)

log_handler = TqdmLoggingHandler()
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))

logger = logging.getLogger(__name__)
logger.addHandler(log_handler)
logger.setLevel(logging.INFO)
logger.propagate = False

TRANSCODERS = {
    'wav': {
        'command_base': ['flac', '--keep-foreign-metadata', '--best', '--preserve-modtime', '--verify'],
        'input_ext': '.wav',
        'output_ext': '.flac',
        'output_flag': '-o',
        'find_pattern': '*.wav',
        'delete_source': True
    },
    'mov': {
        'command_base': ['ffmpeg', '-y', '-i'],
        'input_ext': '.mov',
        'output_ext': '.mkv',
        'find_pattern': '*.[mM][oO][vV]',
        'delete_source': True,
        'extra_args': ['-map', '0', '-c:v', 'ffv1', '-level', '3', '-g', '1', '-c:a', 'flac'],
        'validate_output': True  # Check if size > 0
    }
}

def parse_args() -> argparse.Namespace:
    parser = Parser(description='Transcode media files within AMI packages.')
    parser.add_package()
    parser.add_packagedirectory()
    parser.add_argument(
        '--type',
        choices=['wav', 'mov'],
        help='Optional. Restrict transcoding to a specific file type (wav or mov).'
    )
    
    return parser.parse_args()

def parse_manifest(pkg_path: Path) -> dict:
    """Reads standard bagit/AMI manifests into a dictionary mapping resolved paths to (algorithm, hash)."""
    manifest_data = {}
    for manifest_name in ['manifest-md5.txt']:
        manifest_path = pkg_path / manifest_name
        if manifest_path.exists():
            hash_algo = 'md5' if 'md5' in manifest_name else 'sha256'
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        parts = line.strip().split(maxsplit=1)
                        if len(parts) == 2:
                            file_hash, file_rel_path = parts
                            file_rel_path = file_rel_path.lstrip('*')
                            full_path = (pkg_path / file_rel_path).resolve()
                            manifest_data[full_path] = (hash_algo, file_hash.lower())
            except Exception as e:
                logger.error(f"Failed to read manifest {manifest_path.name}: {e}")
    return manifest_data

def verify_file_checksum(file_path: Path, manifest_data: dict) -> bool:
    """Hashes the file and compares it against the manifest data."""
    resolved_path = file_path.resolve()
    if resolved_path not in manifest_data:
        logger.warning(f"File not found in manifest, skipping pre-validation: {file_path.name}")
        return True 
        
    algo, expected_hash = manifest_data[resolved_path]
    hasher = hashlib.md5() if algo == 'md5' else hashlib.sha256()
    
    logger.info(f"Validating {algo.upper()} checksum for {file_path.name}...")
    try:
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096 * 1024), b""):
                hasher.update(chunk)
        actual_hash = hasher.hexdigest().lower()
        
        if actual_hash == expected_hash:
            return True
        else:
            logger.error(f"Checksum mismatch for {file_path.name}. Expected: {expected_hash}, Got: {actual_hash}")
            return False
    except Exception as e:
        logger.error(f"Error reading file for checksum {file_path.name}: {e}")
        return False

def generate_framemd5(file_path: Path) -> Path:
    """Generates a framemd5 file for a given media file using ffmpeg."""
    out_fmd5 = file_path.with_suffix('.framemd5')
    cmd = ['ffmpeg', '-v', 'error', '-y', '-i', str(file_path), '-f', 'framemd5', str(out_fmd5)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return out_fmd5
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to generate framemd5 for {file_path.name}: {e.stderr}")
        if out_fmd5.exists():
            out_fmd5.unlink()
        return None

def compare_framemd5(fmd5_1: Path, fmd5_2: Path) -> bool:
    """Compares the payload hashes of two framemd5 files, ignoring header lines."""
    def extract_hashes(fmd5_path: Path) -> list[str]:
        hashes = []
        with open(fmd5_path, 'r', encoding='utf-8') as f:
            for line in f:
                if not line.startswith('#'):
                    hashes.append(line.strip())
        return hashes
        
    try:
        hashes1 = extract_hashes(fmd5_1)
        hashes2 = extract_hashes(fmd5_2)
        
        if not hashes1 or not hashes2:
            logger.error("One or both framemd5 files are empty or unreadable.")
            return False
            
        return hashes1 == hashes2
    except Exception as e:
        logger.error(f"Error reading framemd5 files during comparison: {e}")
        return False

def transcode_single_file(input_file: Path, output_file: Path, config: dict) -> bool:
    command = config['command_base'] + [str(input_file)]
    
    if 'extra_args' in config:
        command += config['extra_args']
        
    if 'output_flag' in config:
        command += [config['output_flag'], str(output_file)]
    else:
        command += [str(output_file)]
    
    try:
        subprocess.run(command, capture_output=True, text=True, check=True)
        
        if config.get('validate_output'):
            if not (output_file.exists() and output_file.stat().st_size > 0):
                logger.error(f"Transcoding returned success but output file {output_file.name} is missing or 0 bytes.")
                return False
        
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Transcoding failed for {input_file.name}: {e.stderr}")
        return False

def find_files(pkg_path: Path, config: dict) -> list[Path]:
    files = []
    
    pm_dir = pkg_path / 'data' / 'PreservationMasters'
    em_dir = pkg_path / 'data' / 'EditMasters'
    
    for main_dir in [pm_dir, em_dir]:
        if main_dir.exists() and main_dir.is_dir():
            pattern = config['find_pattern']
            files.extend([f for f in main_dir.rglob(pattern) if not f.name.startswith("._")])
    
    return files

def process_file_worker(input_file: Path, config: dict, manifest_data: dict) -> tuple[Path, bool]:
    output_file = input_file.with_suffix(config['output_ext'])
    
    if output_file.exists():
        logger.info(f"Output file already exists, skipping: {output_file.name}")
        return output_file, True
        
    # validate checksum
    if not verify_file_checksum(input_file, manifest_data):
        logger.error(f"Aborting transcode for {input_file.name} due to checksum failure.")
        return output_file, False
        
    # transcode
    logger.info(f"Transcoding {input_file.name} to {output_file.name}")
    success = transcode_single_file(input_file, output_file, config)
    
    if success:
        logger.info(f"Transcode complete. Validating lossless integrity for {output_file.name}...")
        
        # framemd5 check
        src_fmd5 = generate_framemd5(input_file)
        dst_fmd5 = generate_framemd5(output_file)
        
        is_lossless = False
        if src_fmd5 and dst_fmd5:
            is_lossless = compare_framemd5(src_fmd5, dst_fmd5)
            
        if src_fmd5 and src_fmd5.exists(): src_fmd5.unlink()
        if dst_fmd5 and dst_fmd5.exists(): dst_fmd5.unlink()
        
        if is_lossless:
            logger.info(f"Lossless verification passed for: {output_file.name}")
            if config.get('delete_source'):
                input_file.unlink()
                logger.info(f"Deleted source: {input_file.name}")
        else:
            logger.error(f"Lossless verification FAILED. Source streams do not match output for: {input_file.name}")
            if output_file.exists():
                output_file.unlink()
                logger.info(f"Deleted failed transcoded output: {output_file.name}")
            success = False
    else:
        logger.error(f"Failed to transcode: {input_file.name}")
        if output_file.exists():
            output_file.unlink()
            
    return output_file, success

def run_transcoding(args: argparse.Namespace, type_filter: str = None) -> None:
    if getattr(args, 'packages', None) is None:
        logger.warning("No package or directory provided. Please use --package or --directory.")
        return

    dir_pattern = re.compile(r"^\d{6}$")
    potential_packages = set()

    for p in args.packages:
        if dir_pattern.match(p.name):
            potential_packages.add(p)
        elif p.is_dir():
            for child in p.iterdir():
                if child.is_dir() and dir_pattern.match(child.name):
                    potential_packages.add(child)

    valid_packages = sorted(list(potential_packages))

    if not valid_packages:
        logger.warning("No valid package directories (matching 6 digits) found to process.")
        return

    logger.info(f"Found {len(valid_packages)} valid package(s) to process. Scanning for files...")

    active_types = [type_filter] if type_filter else ([args.type] if args.type else TRANSCODERS.keys())
    
    all_tasks = []
    for pkg in tqdm(valid_packages, desc="Scanning packages", unit="pkg", disable=len(valid_packages) < 5):
        manifest_data = parse_manifest(pkg)
        for t in active_types:
            config = TRANSCODERS[t]
            files = find_files(pkg, config)
            for f in files:
                all_tasks.append((f, config, manifest_data))
        
    if not all_tasks:
        logger.info("No files found to transcode in any of the provided packages.")
        return

    workers = 1 # os.cpu_count() or 4
    logger.info(f"Initiating batch transcoding for {len(all_tasks)} files using {workers} workers.")

    success_count = 0
    failure_count = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_file_worker, f, cfg, manifest): f for f, cfg, manifest in all_tasks}
        with tqdm(total=len(futures), desc="Transcoding files", unit="file") as pbar:
            for future in as_completed(futures):
                input_file = futures[future]
                try:
                    _, success = future.result()
                    if success:
                        success_count += 1
                    else:
                        failure_count += 1
                except Exception as e:
                    logger.error(f"Exception raised while processing {input_file.name}: {e}")
                    failure_count += 1
                pbar.update(1)

    logger.info(f"Processing completed: {success_count} successful, {failure_count} failed.")

def main() -> None:
    args = parse_args()
    
    type_filter = None
    cmd_name = Path(sys.argv[0]).name
    if 'wav_to_flac' in cmd_name:
        type_filter = 'wav'
    elif 'mov_to_mkv' in cmd_name:
        type_filter = 'mov'
        
    run_transcoding(args, type_filter=type_filter)

if __name__ == '__main__':
    main()