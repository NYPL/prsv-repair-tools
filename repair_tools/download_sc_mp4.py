import argparse
from repair_tools.utils.format_utils import print_standard_summary
from repair_tools.utils.cli import extant_dir
import logging
import os
import re
import boto3
import datetime
import json
import shutil
import subprocess
from pathlib import Path
from multiprocessing import Pool
from concurrent.futures import ThreadPoolExecutor, as_completed

import repair_tools.ami_scripts_imports.video_processing as vp
import repair_tools.ami_scripts_imports.audio_to_mp4_converter as amp4
import repair_tools.utils.aws_utils as aws_utils

from repair_tools.utils.logger_setup import setup_logging

################# 

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--package",
        "-p",
        type=extant_dir,
        help="Path to the AMI package missing service copies"
    )
    parser.add_argument(
        "--directory",
        "-d",
        type=extant_dir,
        help="Path to directory of AMI packages missing service copies"
    )
    parser.add_argument(
        "--bucket",
        "-b",
        type=str,
        default="ami-carnegie-servicecopies",
        help="AWS S3 bucket name"
    )
    parser.add_argument(
        "--profile",
        type=str,
        help="AWS S3 profile name"
    )
    parser.add_argument(
        "--test",
        action='store_true',
        help="flag to mock S3 interactions and moves for testing purposes"
    )
    parser.add_argument(
        "--index_path", 
        type=Path,
        help="Path to JSON index of bucket contents"
    )
    parser.add_argument(
        "--logpath",
        type=Path,
        default=Path.home() / "Documents/download_sc_logs",
        help="Directory to store logs"
    )
    
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--download-only",
        "-dl",
        action="store_true",
        help="Only attempt S3 downloads. Do not fallback to transcoding."
    )
    group.add_argument(
        "--transcode-only",
        "-tr",
        action="store_true",
        help="Force local transcoding. Do not check S3."
    )

    return parser.parse_args()

################# 

DEFAULT_INDEX_PATH = Path.home() / "Documents/s3_index.json"
NUM_CPU_THREADS = os.cpu_count() or 4
NUM_DOWNLOAD_THREADS = 8

################# 

def json_datetime_serializer(obj):
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    raise TypeError(f"Type {type(obj)} not serializable")

################# 

def find_pm_files(pkg_dir: Path) -> list[Path]:
    pm_dir = pkg_dir / 'data' / 'PreservationMasters'
    if not pm_dir.exists():
        return []
    
    valid_exts = {'.mkv', '.mov', '.dv', '.flac', '.wav', '.iso'}
    return [p for p in pm_dir.rglob('*') if p.suffix.lower() in valid_exts and not p.name.startswith('._')]

def get_media_duration(file_path: Path) -> float:
    """Uses ffprobe to extract duration of a media file."""
    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(file_path)
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logging.error(f"Failed to get duration for {file_path.name}: {e}")
        return -1.0

def verify_transcode_success(pm_path: Path, sc_path: Path) -> bool:
    """Confirms the output file is not empty and matches duration of the source file."""
    if not sc_path.exists() or sc_path.stat().st_size == 0:
        logging.error(f"Verification failed: {sc_path.name} does not exist or is empty.")
        return False
        
    pm_duration = get_media_duration(pm_path)
    sc_duration = get_media_duration(sc_path)
    
    if pm_duration < 0 or sc_duration < 0:
        logging.error(f"Verification failed: Could not retrieve duration for {pm_path.name} or {sc_path.name}.")
        return False
        
    # Duration comparison within a 0.5s margin to account for container differences
    duration_diff = abs(pm_duration - sc_duration)
    if duration_diff > 0.5:
        logging.error(f"Verification failed: Duration mismatch for {sc_path.name}. PM: {pm_duration}s, SC: {sc_duration}s (diff: {duration_diff}s)")
        return False
        
    logging.info(f"Verification successful for {sc_path.name}. Duration diff: {duration_diff:.3f}s")
    return True

def create_sc_worker(task):
    pkg_path, files_to_process = task
    errors = []
    successful_files = []
    
    try:
        sc_dir = pkg_path / 'data' / 'ServiceCopies'
        try:
            sc_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.warning(f"Failed to create SC directory {sc_dir}: {e}")
            pass

        if not files_to_process:
             return pkg_path, [], ["No PM files found to transcode worker"]

        for file in files_to_process:
            file_path = Path(file)
            print(f"Starting transcode worker for {file_path.name}")
            
            sc_name = file_path.stem.replace('_pm', '_sc') + ".mp4"
            em_name = file_path.stem.replace('_pm', '_em') + ".mp4"
            
            if (sc_dir / sc_name).exists():
                successful_files.append(sc_name)
                print(f"Service copy already exists: {sc_name}")
                continue
            if (sc_dir / em_name).exists():
                successful_files.append(em_name) 
                print(f"Service copy already exists: {em_name}")
                continue

            try:
                if file_path.suffix.lower() in {'.wav', '.flac'}:
                    target_sc_path = sc_dir / sc_name
                    amp4.convert_audio(str(file_path), "mp4", output_file=str(target_sc_path))
                else:
                    vp.convert_to_mp4(file_path.resolve(), file_path.name, sc_dir, audio_pan="auto")
                
                created_path = None
                if (sc_dir / sc_name).exists():
                    created_path = sc_dir / sc_name
                elif (sc_dir / em_name).exists():
                    created_path = sc_dir / em_name
                elif (sc_dir / file_path.with_suffix('.mp4').name).exists():
                    created_path = sc_dir / file_path.with_suffix('.mp4').name
                    try:
                        created_path = created_path.rename(sc_dir / sc_name)
                    except Exception as e:
                        logging.error(f"Failed to rename created file {created_path} to {sc_name}: {e}")
                        created_path = sc_dir / sc_name

                if created_path and verify_transcode_success(file_path, created_path):
                    successful_files.append(created_path.name)
                    print(f"Successfully created and verified service copy: {created_path.name}")
                else:
                    if created_path and created_path.exists():
                        try:
                            created_path.unlink()
                        except Exception as e:
                            logging.error(f"Failed to delete invalid output file {created_path.name}: {e}")
                    raise RuntimeError(f"Verification failed for service copy of {file_path.name}")
                    
            except Exception as e:
                errors.append(f"Transcode failed for {file_path.name}: {e}")
        print(f"Finished transcode worker for {pkg_path.name}")    
        return pkg_path, successful_files, errors
    except Exception as e:
        return pkg_path, [], [f"Exception: {str(e)}"]
    
def build_s3_index(bucket_name, index_path):
    if index_path.exists():
        creation_date = index_path.stat().st_ctime
        now = datetime.datetime.now().timestamp()
        if now - creation_date > 60 * 60 * 24 * 7: # 1 week
            logging.info(f"Cached index is out of date. Refreshing...")
            index_path.unlink()
            return build_s3_index(bucket_name, index_path)
        logging.info(f"Loading cached index from {index_path}.")
        with open(index_path, 'r') as f:
            raw_list = json.load(f)
    else:
        logging.info(f"Index not found. Scanning bucket {bucket_name}.")
        s3 = boto3.client('s3')
        paginator = s3.get_paginator('list_objects_v2')
        raw_list = []
        for page in paginator.paginate(Bucket=bucket_name):
            raw_list.extend(page.get('Contents', []))
        
        # Save cache
        logging.info(f"Caching index to {index_path}...")
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with open(index_path, 'w') as f:
            json.dump(raw_list, f, indent=2, default=json_datetime_serializer)

    print(f"S3 bucket index saved to {index_path} with {len(raw_list)} objects.")
    
    index_map = {}
    for obj in raw_list:
        key = obj['Key']
        filename = Path(key).name
        index_map[filename] = key
    
    return index_map
    
def download_file_worker(task):
    bucket, key, dest, pkg_path, pm_filename, test_mode = task
    
    if dest.exists():
        return pkg_path, pm_filename, f"Skipped (SC Exists): {dest.name}", True

    if test_mode:
        return pkg_path, pm_filename, f"[TEST] Would download {key} to {dest}", True

    try:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logging.warning(f"Failed to create SC directory {dest.parent}: {e}")
            return pkg_path, pm_filename, f"Failed to create directory for {dest.parent}: {e}", False
        try:
            dest.parent.chmod(0o777)
        except Exception as e:
            logging.warning(f"Failed to set permissions for {dest.parent}: {e}")
            pass
        s3 = boto3.client('s3')
        s3.download_file(bucket, key, str(dest))
        
        pm_path = pkg_path / 'data' / 'PreservationMasters' / pm_filename
        if not pm_path.exists():
            pm_path = pkg_path / 'data' / 'EditMasters' / pm_filename
            
        if pm_path.exists():
            if not verify_transcode_success(pm_path, dest):
                if dest.exists():
                    dest.unlink()
                return pkg_path, pm_filename, f"Error: Downloaded file {dest.name} failed duration/validity verification", False
        else:
            logging.warning(f"Could not find local source file to verify download duration for {dest.name}")
            
        return pkg_path, pm_filename, f"{key} found in bucket, downloaded, and verified: {dest.name}", True
    except Exception as e:
        return pkg_path, pm_filename, f"Error downloading {key}: {e}", False

def print_summary(stats, package_status):
    total_sc_secured = stats["dl_success"] + stats["tc_success"]
    match = total_sc_secured == stats["pm_found"]
    
    summary_dict = {
        "Total Packages Scanned": stats["total"],
        "PM Files Found": stats["pm_found"],
        "SC Files Secured": total_sc_secured,
        "Overall Status": "MATCH (All PMs have an SC)" if match else f"MISMATCH (Expected {stats['pm_found']}, got {total_sc_secured})",
        "S3 Downloads": stats["dl_success"],
        "Local Transcodes": stats["tc_success"],
        "Failed Downloads": stats["dl_fail"],
        "Failed Transcodes": stats["tc_fail"],
        "Packages Moved to Fixed": stats["moved"]
    }
    
    failed_pkgs = [p for p, data in package_status.items() if len(data["secured_pms"]) != len(data["expected_pms"])]
    if failed_pkgs:
        summary_dict["Failed Packages"] = {}
        for p in failed_pkgs:
            data = package_status[p]
            missing_files = data["expected_pms"] - data["secured_pms"]
            secured_count = len(data["secured_pms"])
            total_count = len(data["expected_pms"])
            
            summary_dict["Failed Packages"][p] = f"{secured_count}/{total_count} files secured"
            if missing_files:
                summary_dict["Failed Packages"][f"  Missing in {p}"] = list(missing_files)
            if data.get("errors"):
                summary_dict["Failed Packages"][f"  Errors in {p}"] = data["errors"]
                
    print_standard_summary("Download/Transcode Summary", summary_dict)

def main():

    args = parse_args()

    if not args.logpath.exists():
        args.logpath.mkdir(parents=True, exist_ok=True)
    setup_logging(args.logpath / f"download_sc_{datetime.datetime.now():%Y%m%d}.log", noisy_loggers=['repair_tools', 'ffmpeg'])

    pkg_paths = []
    if args.package:
        pkg_paths.append(Path(args.package))
    else: 
        dir_pattern = re.compile(r"^\d{6}$")
        pkg_paths = [d for d in Path(args.directory).iterdir() if d.is_dir() and dir_pattern.match(d.name)]

    if not pkg_paths:
        logging.warning("No package directories found.")
        return

    logging.info(f"Found {len(pkg_paths)} package(s) to process.")

    s3_lookup = {}
    use_s3 = (args.bucket and args.profile) and not args.transcode_only

    if use_s3:
        aws_utils.validate_aws_sso(args.profile)

        try:
            boto3.setup_default_session(profile_name=args.profile)
            index_path = args.index_path if args.index_path else DEFAULT_INDEX_PATH
            s3_lookup = build_s3_index(args.bucket, index_path)
        except Exception as e:
            logging.error(f"Failed to log into S3 session: {e}. All packages will fall back to transcoding.")
            use_s3 = False

    stats = {
        'total': len(pkg_paths),
        'pm_found': 0,
        'dl_success': 0,
        'dl_fail': 0,
        'tc_success': 0,
        'tc_fail': 0,
        'moved': 0
    }
    
    package_status = {
        p: {'expected_pms': set(), 'secured_pms': set(), 'errors': []} for p in pkg_paths
    }

    download_tasks = []     
    transcode_map = {} 

    for pkg in pkg_paths:
        pm_files = find_pm_files(pkg)
        stats['pm_found'] += len(pm_files)

        if not pm_files:
            logging.warning(f"Skipping {pkg.name}: No PM files found.")
            package_status[pkg]['errors'].append("No PM files found")
            continue

        for pm_file in pm_files:
            package_status[pkg]['expected_pms'].add(pm_file.name)
            
            sc_dir = pkg / 'data' / 'ServiceCopies'
            found_key = None
        
            if use_s3 and not args.transcode_only:
                candidates = [
                    pm_file.stem.replace('_pm', '_sc') + ".mp4",
                    pm_file.stem.replace('_pm', '_em').replace('_sc', '_em') + ".mp4"
                ]
                
                for cand in candidates:
                    if cand in s3_lookup:
                        found_key = s3_lookup[cand]
                        break
                
            if found_key and not args.transcode_only:
                    dest = sc_dir / Path(found_key).name
                    download_tasks.append((args.bucket, found_key, dest, pkg, pm_file.name, args.test))
            elif not args.download_only:
                if pm_file.suffix.lower() == '.iso':
                    package_status[pkg]['errors'].append(f"ISO transcoding is not supported: {pm_file.name}")
                else:
                    if pkg not in transcode_map:
                        transcode_map[pkg] = []
                    transcode_map[pkg].append(pm_file)
            else:
                package_status[pkg]['errors'].append(f"Missing from S3: {pm_file.name}")

    if len(download_tasks) == 0:
        logging.info("No files to download from S3, proceeding to transcode check.")
    else:
        logging.info(f"Starting {len(download_tasks)} downloads using {NUM_DOWNLOAD_THREADS} threads...")
        with ThreadPoolExecutor(max_workers=NUM_DOWNLOAD_THREADS) as executor:
            futures = [executor.submit(download_file_worker, task) for task in download_tasks]
            
            for future in as_completed(futures):
                pkg, pm_name, msg, success = future.result()
                if success:
                    logging.info(msg)
                    stats['dl_success'] += 1
                    package_status[pkg]['secured_pms'].add(pm_name)
                else:
                    logging.error(msg)
                    stats['dl_fail'] += 1
                    package_status[pkg]['errors'].append(msg)

    transcode_tasks = [(pkg, files) for pkg, files in transcode_map.items()]
    
    if transcode_tasks:
        total_files = sum(len(files) for _, files in transcode_tasks)
        
        if args.test:
            logging.info(f"[TEST MODE] Would transcode: {total_files} files for {len(transcode_tasks)} packages")
            stats['tc_success'] += total_files
            for pkg, files in transcode_tasks:
                for f in files:
                    package_status[pkg]['secured_pms'].add(f.name)
        else:
            logging.info(f"Starting transcode for {total_files} files for {len(transcode_tasks)} packages: [{' '.join(str(t[0].name) for t in transcode_tasks)}]")
            with Pool(processes=NUM_CPU_THREADS) as pool:
                results = pool.map(create_sc_worker, transcode_tasks)

            for pkg, success_list, errors in results:
                if isinstance(success_list, list):
                    count = len(success_list)
                    for sc_file in success_list:
                        sc_stem = Path(sc_file).stem
                        matched = False
                        for expected_pm in package_status[pkg]['expected_pms']:
                            pm_stem = Path(expected_pm).stem
                            base_sc = sc_stem.replace('_sc', '').replace('_em', '')
                            base_pm = pm_stem.replace('_pm', '')
                            if base_sc == base_pm or base_sc.startswith(base_pm):
                                package_status[pkg]['secured_pms'].add(expected_pm)
                                matched = True
                                break
                else:
                    count = 0

                stats['tc_success'] += count
                stats['tc_fail'] += len(errors)
                package_status[pkg]['errors'].extend(errors)
                
                if errors:
                    logging.error(f"Transcode errors for {pkg.name}: {errors}")

    fixed_dir = pkg_paths[0].parent.parent / '_sc_fixed'
    iso_false_dir = pkg_paths[0].parent.parent / '_iso'
    successful_pkgs_to_move = []

    for pkg, data in package_status.items():
        expected_count = len(data['expected_pms'])
        secured_count = len(data['secured_pms'])
        
        if expected_count > 0 and expected_count == secured_count:
            successful_pkgs_to_move.append(pkg)
        elif expected_count > 0:
            logging.warning(f"FAILURE {pkg.name}: Expected {expected_count}, got {secured_count}. Errors: {data['errors']}")
    
    if successful_pkgs_to_move:
        if not args.test:
            fixed_dir.mkdir(parents=True, exist_ok=True)
            for pkg in successful_pkgs_to_move:
                try:
                    dest_path = fixed_dir / pkg.parent.name / pkg.name
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(pkg), str(dest_path))
                    logging.info(f"Moved {pkg.name} to {dest_path}")
                    stats['moved'] += 1
                except Exception as e:
                    logging.error(f"Failed to move {pkg.name}: {e}")
        else:
            logging.info(f"[TEST] Would move {len(successful_pkgs_to_move)} packages to {fixed_dir}")
            stats['moved'] = len(successful_pkgs_to_move)

    # Identify and move any leftover packages containing ISO files to the _iso directory
    iso_packages_to_move = []
    for pkg in pkg_paths:
        if pkg in successful_pkgs_to_move:
            continue
        pm_files = find_pm_files(pkg)
        if any(f.suffix.lower() == '.iso' for f in pm_files):
            iso_packages_to_move.append(pkg)

    if iso_packages_to_move:
        if not args.test:
            iso_false_dir.mkdir(parents=True, exist_ok=True)
            for pkg in iso_packages_to_move:
                try:
                    dest_path = iso_false_dir / pkg.parent.name / pkg.name
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(pkg), str(dest_path))
                    logging.info(f"Moved ISO package {pkg.name} to {dest_path}")
                    stats['moved'] += 1
                except Exception as e:
                    logging.error(f"Failed to move ISO package {pkg.name}: {e}")
        else:
            logging.info(f"[TEST] Would move {len(iso_packages_to_move)} ISO packages to {iso_false_dir}")
            stats['moved'] += len(iso_packages_to_move)

    source_dir = Path(args.directory) if args.directory else Path(args.package).parent

    print_summary(stats, package_status)
    
    if source_dir.exists():
        visible_files = [p for p in source_dir.iterdir() if not p.name.startswith('.')]
        if not visible_files:
            if args.test:
                 logging.info(f"[TEST] Would remove empty source directory {source_dir.name}")
            else:
                try:
                    shutil.rmtree(source_dir)
                    logging.info("Empty source directory removed.")
                except Exception as e:
                    logging.error(f"Failed to remove empty source directory: {e}")
                    
                


if __name__ == "__main__":
    main()