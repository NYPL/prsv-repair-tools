import os
import re
import subprocess
import argparse
import sys
import logging
from datetime import datetime
from pathlib import Path

try:
    from repair_tools.utils.logger_setup import setup_logging
except ImportError:
    print("Error: Could not import 'logger_setup.py'.")
    sys.exit(1)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--directory", 
        help="The root directory to scan."
    )
    parser.add_argument(
        "--dry-run", 
        action="store_true", 
        help="Print commands without executing."
    )
    parser.add_argument(
        "--overwrite", 
        action="store_true", 
        help="Overwrite existing output files."
    )
    parser.add_argument(
        "--log_path", 
        type=Path, 
        default=Path(f"logs/mix_streams_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"), 
        help="Path to the log file (default: mix_streams_[YYYYMMDD_HHMMSS].log in current dir)."
    )
    return parser.parse_args()

def has_video_stream(filepath):
    cmd = [
        'ffprobe', 
        '-v', 'error', 
        '-select_streams', 'v:0', 
        '-show_entries', 'stream=codec_type', 
        '-of', 'csv=p=0', 
        filepath
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return 'video' in result.stdout.lower()
    except subprocess.CalledProcessError:
        return False

def group_files_by_stream(directory, stream_pattern, ignore_extensions):
    groups = {}
    
    for filename in os.listdir(directory):
        if any(filename.endswith(ext) for ext in ignore_extensions):
            continue
        
        if filename.startswith('.'):
            continue

        match = stream_pattern.match(filename)
        if match:
            prefix = match.group(1)
            # stream_id = match.group(2) # unused
            suffix = match.group(3)
            
            # Key is the filename without the s## part
            base_key = f"{prefix}{suffix}"
            full_path = os.path.join(directory, filename)
            
            if base_key not in groups:
                groups[base_key] = []
            groups[base_key].append(full_path)
            
    return groups

def run_ffmpeg_mix(output_path, input_files, logger, dry_run=False):
    input_files.sort()
    num_inputs = len(input_files)
    
    # 1. Check for video streams
    if not dry_run:
        for f in input_files:
            if has_video_stream(f):
                raise RuntimeError(f"Video stream detected in {f}. This script is for audio-only mixing.")

    # 2. Codec based on output extension
    ext = os.path.splitext(output_path)[1].lower()
    if ext == '.flac':
        codec_args = ['-c:a', 'flac']
    elif ext == '.mp4':
        codec_args = ['-c:a', 'aac', '-b:a', '320k']
    elif ext == '.wav':
        codec_args = ['-c:a', 'pcm_s24le']
    else:
        codec_args = []

    # 3. FFMPEG command 
    cmd = ['ffmpeg', '-y']
    
    for f in input_files:
        cmd.extend(['-i', f])
    
    # Mix inputs + restore volume
    input_labels = "".join([f"[{i}:a]" for i in range(num_inputs)])
    filter_chain = (
        f"{input_labels}"
        f"amix=inputs={num_inputs}:duration=longest:dropout_transition=0,"
        f"volume={num_inputs}"
        f"[out]"
    )
    
    cmd.extend(['-filter_complex', filter_chain])
    cmd.extend(['-map', '[out]'])
    cmd.extend(codec_args)
    cmd.append(output_path)
    
    logger.info(f"Mixing {num_inputs} streams -> {os.path.basename(output_path)}")

    if dry_run:
        logger.info(f"[DRY RUN] Command: {' '.join(cmd)}")
    else:
        try:
            subprocess.run(cmd, check=True, stderr=subprocess.PIPE)
            logger.info(f"[SUCCESS] Created {output_path}")
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode('utf-8')
            logger.error(f"FFmpeg failed for {output_path}")
            logger.error(f"FFmpeg Error Output: {error_msg}")
            raise RuntimeError("FFmpeg encoding failed") from e

def main():
    allowed_folders = {'PreservationMasters', 'ServiceCopies'}
    
    stream_pattern = re.compile(r"^(.*)(s\d+)(_.*)$")
    
    ignore_extensions = ['.json', '.txt', '.xml', '.md5', '.old', '.DS_Store']

    args = parse_args()
    
    logger, list_logger = setup_logging(args.log_path)
    
    root_dir = Path(args.directory)
    
    if not root_dir.exists():
        logger.error(f"Directory {root_dir} does not exist.")
        sys.exit(1)

    logger.info(f"Scanning directory: {root_dir}")
    if args.dry_run:
        logger.info("--- DRY RUN MODE ---")

    # Tracking sets
    processed_packages = set()
    failed_package_ids = set()

    for current_root, dirs, files in os.walk(root_dir):
        folder_name = os.path.basename(current_root)
        
        if folder_name not in allowed_folders:
            continue
            
        try:
            bag_id = Path(current_root).parents[1].name
        except IndexError:
            bag_id = "UNKNOWN_BAG"
            
        grouped_files = group_files_by_stream(current_root, stream_pattern, ignore_extensions)
        
        for output_filename, input_paths in grouped_files.items():
            if len(input_paths) > 1:
                # Mark this package as "encountered/processed"
                processed_packages.add(bag_id)
                
                output_full_path = os.path.join(current_root, output_filename)
                
                if os.path.exists(output_full_path) and not args.overwrite:
                    logger.info(f"{output_filename} already exists. Use --overwrite to replace.")
                    continue
                
                try:
                    run_ffmpeg_mix(output_full_path, input_paths, logger, dry_run=args.dry_run)
                except Exception as e:
                    logger.error(f"Error processing {output_filename}: {e}")
                    failed_package_ids.add(bag_id)
                    continue

    successful_count = len(processed_packages) - len(failed_package_ids)
    
    logger.info("-" * 40)
    logger.info("SUMMARY")
    logger.info("-" * 40)
    
    logger.info(f"Packages Processed: {len(processed_packages)} "
                f"[Successful: {successful_count}, Failed: {len(failed_package_ids)}]")
    
    if failed_package_ids:
        failed_list = sorted(list(failed_package_ids))
        logger.info(f"Failed Packages: {failed_list}")
    else:
        logger.info("Failed Packages: []")

if __name__ == "__main__":
    main()