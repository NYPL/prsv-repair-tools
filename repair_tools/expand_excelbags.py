import argparse
import logging
import shutil
import json
import re
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from pathlib import Path

from repair_tools.create_bag_structure import create_dir_structure
from repair_tools.utils.format_utils import print_standard_summary
from repair_tools.utils.cli import Parser
from repair_tools.get_md5 import calculate_md5

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def parse_args() -> argparse.Namespace:
    parser = Parser()
    parser.add_package()
    parser.add_argument(
        "--logpath",
        type=Path,
        default=Path("logs"),
        help="Directory for log files."
    )
    parser.add_argument(
        "--debug",
        action="store_true"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Number of concurrent threading workers."
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="Moves files to the new package structure. If omitted, files will be copied safely."
    )
    return parser.parse_args()

def find_column_by_keywords(df_columns, keywords: list[str]):
    for col in df_columns:
        # normalize potential headers
        normalized_col = re.sub(r'[\W_]+', '', str(col).lower())
        for kw in keywords:
            normalized_kw = re.sub(r'[\W_]+', '', kw.lower())
            if normalized_kw in normalized_col:
                return col
    return None

def get_tab_priority(sheet_name: str) -> int:
    """
    Returns a priority weight for sorting. Higher number = Higher priority.
    """
    normalized_name = re.sub(r'[\W_]+', '', sheet_name.lower())
    
    if "preservationmaster" in normalized_name:
        return 4
    elif "editmaster" in normalized_name:
        return 3
    elif "servicecopy" in normalized_name:
        return
    return 1  

def generate_sidecar_json(row_data: pd.Series, output_path: Path, filename: str):
    if output_path.exists():
        return

    cols = row_data.index

    # phrases for fuzzy matching
    id_col = find_column_by_keywords(cols, ["class mark", "classmark", "id", "primary id"])
    barcode_col = find_column_by_keywords(cols, ["barcode", "bar code"])
    cms_col = find_column_by_keywords(cols, ["cms collection", "collection id", "cms id"])
    title_col = find_column_by_keywords(cols, ["title", "object name", "description"])
    role_col = find_column_by_keywords(cols, ["file role", "role", "asset role"])
    codec_col = find_column_by_keywords(cols, ["codec", "audio codec"])
    cue_col = find_column_by_keywords(cols, ["cue", "cue file", "cue filename"])
    date_col = find_column_by_keywords(cols, ["date created", "creation date", "date"])
    dur_human_col = find_column_by_keywords(cols, ["duration human", "human duration", "runtime"])
    dur_ms_col = find_column_by_keywords(cols, ["duration milli", "duration ms", "milliseconds"])
    size_col = find_column_by_keywords(cols, ["file size", "bytes", "size"])
    field_col = find_column_by_keywords(cols, ["sound field", "audio sound field", "stereo"])
    format_col = find_column_by_keywords(cols, ["format", "physical format"])
    type_col = find_column_by_keywords(cols, ["object type", "type"])
    vol_col = find_column_by_keywords(cols, ["volume number", "volume", "vol"])
    diam_col = find_column_by_keywords(cols, ["diameter", "gauge"])
    dye_col = find_column_by_keywords(cols, ["dye", "dye layer"])
    reflect_col = find_column_by_keywords(cols, ["reflective", "reflective layer"])
    mfg_col = find_column_by_keywords(cols, ["stock manufacturer", "manufacturer", "mfg"])
    face_col = find_column_by_keywords(cols, ["face number", "face", "side"])
    
    # Software & Capture Hardware lookups
    soft_mfg_col = find_column_by_keywords(cols, ["capture software manufacturer", "software brand"])
    soft_plat_col = find_column_by_keywords(cols, ["capture software platform", "os", "platform"])
    soft_name_col = find_column_by_keywords(cols, ["capture software name", "product name", "software"])
    soft_ver_col = find_column_by_keywords(cols, ["capture software version", "software version", "version"])
    dev_mfg_col = find_column_by_keywords(cols, ["playback device manufacturer", "device brand"])
    dev_model_col = find_column_by_keywords(cols, ["playback device model", "device model", "model"])
    dev_serial_col = find_column_by_keywords(cols, ["playback device serial", "device serial", "serial"])
    
    # Operator & Vendor lookups
    op_first_col = find_column_by_keywords(cols, ["operator first name", "operator first", "first name"])
    op_last_col = find_column_by_keywords(cols, ["operator last name", "operator last", "last name"])
    org_city_col = find_column_by_keywords(cols, ["org city", "organization city", "city"])
    org_zip_col = find_column_by_keywords(cols, ["org postal", "org zip", "postal code", "zip"])
    org_state_col = find_column_by_keywords(cols, ["org state", "organization state", "state"])
    org_street_col = find_column_by_keywords(cols, ["org street", "organization street", "address"])
    org_name_col = find_column_by_keywords(cols, ["org name", "organization name", "vendor"])

    # isolation from reference strings if missing structural values
    extracted_id = str(row_data.get(id_col, "unknown")) if id_col else "unknown"
    if extracted_id == "unknown" and "_" in filename:
        parts = filename.split('_')
        if len(parts) > 1 and parts[1].isdigit():
            extracted_id = parts[1]

    json_content = {
        "asset": {
            "fileRole": str(row_data.get(role_col, "pm")) if role_col else "pm", 
            "referenceFilename": filename,
            "schemaVersion": "2.0.0"
        },
        "bibliographic": {
            "barcode": str(row_data.get(barcode_col, "unknown")) if barcode_col else "unknown",
            "cmsCollectionID": str(row_data.get(cms_col, "unknown")) if cms_col else "unknown",
            "divisionCode": "mym",
            "primaryID": extracted_id,
            "projectCode": "MPS",
            "title": str(row_data.get(title_col, "unknown")) if title_col else "unknown",
            "vernacularDivisionCode": "MUS"
        },
        "technical": {
            "audioCodec": str(row_data.get(codec_col, "FLAC")) if codec_col else "FLAC",
            "cueFilename": str(row_data.get(cue_col, "unknown")) if cue_col else "unknown",
            "dateCreated": str(row_data.get(date_col, "unknown")) if date_col else "unknown",
            "durationHuman": str(row_data.get(dur_human_col, "unknown")) if dur_human_col else "unknown",
            "durationMilli": {
                "measure": int(row_data.get(dur_ms_col, 0) or 0) if dur_ms_col else 0,
                "unit": "ms"
            },
            "extension": Path(filename).suffix.lstrip('.'),
            "fileFormat": str(row_data.get(codec_col, "FLAC")) if codec_col else "FLAC",
            "filename": Path(filename).stem,
            "fileSize": {
                "measure": int(row_data.get(size_col, 0) or 0) if size_col else 0,
                "unit": "B"
            }
        },
        "source": {
            "audioRecording": {
                "audioSoundField": str(row_data.get(field_col, "stereo")) if field_col else "stereo"
            },
            "object": {
                "format": str(row_data.get(format_col, "unknown")) if format_col else "unknown",
                "type": str(row_data.get(type_col, "unknown")) if type_col else "unknown",
                "volumeNumber": int(row_data.get(vol_col, 1) or 1) if vol_col else 1
            },
            "physicalDescription": {
                "diameter": {
                    "measure": float(row_data.get(diam_col, 0.0) or 0.0) if diam_col else 0.0,
                    "unit": "in"
                },
                "dyeLayer": str(row_data.get(dye_col, "unknown")) if dye_col else "unknown",
                "reflectiveLayer": str(row_data.get(reflect_col, "unknown")) if reflect_col else "unknown",
                "stockManufacturer": str(row_data.get(mfg_col, "unknown")) if mfg_col else "unknown"
            },
            "subObject": {
                "faceNumber": int(row_data.get(face_col, 1) or 1) if face_col else 1
            }
        },
        "digitizationProcess": {
            "captureSoftware": {
                "manufacturer": str(row_data.get(soft_mfg_col, "unknown")) if soft_mfg_col else "unknown",
                "platform": str(row_data.get(soft_plat_col, "unknown")) if soft_plat_col else "unknown",
                "productName": str(row_data.get(soft_name_col, "unknown")) if soft_name_col else "unknown",
                "version": str(row_data.get(soft_ver_col, "unknown")) if soft_ver_col else "unknown"
            },
            "playbackDevice": {
                "manufacturer": str(row_data.get(dev_mfg_col, "unknown")) if dev_mfg_col else "unknown",
                "model": str(row_data.get(dev_model_col, "unknown")) if dev_model_col else "unknown",
                "serialNumber": str(row_data.get(dev_serial_col, "unknown")) if dev_serial_col else "unknown"
            }
        },
        "digitizer": {
            "operator": {
                "firstName": str(row_data.get(op_first_col, "unknown")) if op_first_col else "unknown",
                "lastName": str(row_data.get(op_last_col, "unknown")) if op_last_col else "unknown"
            },
            "organization": {
                "address": {
                    "city": str(row_data.get(org_city_col, "unknown")) if org_city_col else "unknown",
                    "postalCode": str(row_data.get(org_zip_col, "unknown")) if org_zip_col else "unknown",
                    "state": str(row_data.get(org_state_col, "unknown")) if org_state_col else "unknown",
                    "street1": str(row_data.get(org_street_col, "unknown")) if org_street_col else "unknown"
                },
                "name": str(row_data.get(org_name_col, "unknown")) if org_name_col else "unknown"
            }
        }
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(json_content, f, indent=2)
    logger.info(f"Created JSON: {output_path.name}")

def build_file_index(base_path: Path) -> dict:
    logger.info("Indexing source directories")
    index = {}
    for p in base_path.rglob("*"):
        if p.is_file() and p.suffix not in ['.xlsx', '.json'] and not p.name.startswith("._") and not p.name.startswith("~$"):
            index.setdefault(p.name, []).append(p)
    return index

def query_indexed_files(file_index: dict, token: str) -> list[Path]:
    results = []
    
    pattern = re.compile(rf'(?<!\d){re.escape(token)}(?!\d)')
    
    for fname, paths in file_index.items():
        if pattern.search(fname):
            results.extend(paths)
    return results

def process_single_package(class_id: str, id_rows: pd.DataFrame, base_path: Path, matching_files: list[Path], is_debug: bool, is_move: bool) -> dict:
    stats = {'total': len(matching_files), 'success': 0, 'fail': 0, 'errors': []}
    new_bag_dir = base_path / class_id
    
    if not is_debug and not new_bag_dir.exists():
        # print(f"Will create bag structure in '{new_bag_dir.parent}' named '{new_bag_dir.name}'.")
        create_dir_structure(new_bag_dir.parent, new_bag_dir.name)

    for file_path in matching_files:
        file_success = True
        parent_folder_name = file_path.parent.name
        target_dir = new_bag_dir / "data" / parent_folder_name
        target_file_path = target_dir / file_path.name
        
        if is_debug:
            logger.info(f"[DEBUG] {'Move' if is_move else 'Copy'} staged: {file_path.name} -> {target_dir}")
            debug_dir = base_path / "debug_jsons"
            debug_dir.mkdir(exist_ok=True)
            json_filename = debug_dir / f"DEBUG_{class_id}_{target_file_path.stem}.json"
        else:
            target_dir.mkdir(parents=True, exist_ok=True)
            if not target_file_path.exists():
                try:
                    if is_move:
                        shutil.move(str(file_path), str(target_file_path))
                    else:
                        shutil.copy2(str(file_path), str(target_file_path))
                except Exception as e:
                    stats['errors'].append(f"Failed to {'move' if is_move else 'copy'} {file_path.name}: {e}")
                    file_success = False
            
            json_filename = target_file_path.with_suffix('.json')

        if file_success and not json_filename.exists() and not id_rows.empty:
            try:
                generate_sidecar_json(id_rows.iloc[0], json_filename, target_file_path.name)
            except Exception as e:
                stats['errors'].append(f"Failed to generate JSON {json_filename.name}: {e}")
                file_success = False

        if file_success:
            stats['success'] += 1
        else:
            stats['fail'] += 1

    if not is_debug and stats['fail'] == 0:
        try:
            generate_manifest(new_bag_dir)
        except Exception as e:
            stats['errors'].append(f"Manifest generation failed for {new_bag_dir.name}: {e}")

    return stats

def process_excel_file(excel_file: Path, base_path: Path, is_debug: bool, file_index: dict, max_workers: int, is_move: bool) -> dict:
    logger.info(f"Opening Excel Workbook: {excel_file.name}")
    sheet_stats = {'total': 0, 'success': 0, 'fail': 0, 'errors': []}
    
    try:
        xls = pd.read_excel(excel_file, sheet_name=None, header=None)
    except Exception as e:
        sheet_stats['errors'].append(f"Failed to read {excel_file.name}: {e}")
        return sheet_stats

    # Format: { class_id_clean: (priority_score, id_rows, matching_files, source_tab_name) }
    consolidated_tasks = {}

    for sheet_name, raw_df in xls.items():
        df = raw_df.copy()
        id_column = None
        header_row_index = None

        current_sheet_priority = get_tab_priority(sheet_name)

        for idx, row in df.iterrows():
            row_values = row.dropna().astype(str).str.strip().tolist()
            normalized_row = [re.sub(r'[\W_]+', '', val.lower()) for val in row_values]
            if any(kw in normalized_row for kw in ["classmark", "classmarkid", "primaryid"]) or \
               any(kw in normalized_row for kw in ["referencefilename", "filenameauto", "filename"]):
                header_row_index = idx
                break

        if header_row_index is not None:
            df.columns = df.iloc[header_row_index].str.strip()
            df = df.iloc[header_row_index + 1:].reset_index(drop=True)
        else:
            df.columns = raw_df.iloc[0].str.strip() if not raw_df.empty else []
            df = df.iloc[1:].reset_index(drop=True)

        id_column = find_column_by_keywords(df.columns, ["class mark", "classmark", "id", "cms id"]) or \
                    find_column_by_keywords(df.columns, ["reference filename", "filename auto", "filename"])

        if not id_column:
            logger.info(f"Skipping tab '{sheet_name}' - No valid header found.")
            continue
            
        logger.info(f"Searching for '{id_column}' on sheet '{sheet_name}'.")
        # non empty unique rows:
        unique_ids = df[id_column].dropna().astype(str).unique()
        
        for class_id in unique_ids:
            class_id_clean = class_id.strip()
            if not class_id_clean or class_id_clean.lower() == 'nan':
                continue
                
            id_rows = df[df[id_column].astype(str) == class_id]
            matching_files = query_indexed_files(file_index, class_id_clean)
            
            if matching_files:
                # DEDUPLICATION & PRIORITIZATION LOGIC:
                if class_id_clean in consolidated_tasks:
                    existing_priority = consolidated_tasks[class_id_clean]['priority']
                    if current_sheet_priority > existing_priority:
                        logger.info(f"Overriding metadata for ID {class_id_clean}: Replacing row from tab '{consolidated_tasks[class_id_clean]['tab']}' with higher priority tab '{sheet_name}'.")
                        consolidated_tasks[class_id_clean] = {
                            'priority': current_sheet_priority,
                            'rows': id_rows,
                            'files': matching_files,
                            'tab': sheet_name
                        }
                else:
                    consolidated_tasks[class_id_clean] = {
                        'priority': current_sheet_priority,
                        'rows': id_rows,
                        'files': matching_files,
                        'tab': sheet_name
                    }

    if consolidated_tasks:
        logger.info(f"Spawning concurrent thread workers Pool (Max Threads: {max_workers}) for {len(consolidated_tasks)} prioritized unique packages.")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for class_id_clean, task_data in consolidated_tasks.items():
                futures.append(executor.submit(
                    process_single_package,
                    class_id_clean,
                    task_data['rows'],
                    base_path,
                    task_data['files'],
                    is_debug,
                    is_move
                ))
            
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                sheet_stats['total'] += res['total']
                sheet_stats['success'] += res['success']
                sheet_stats['fail'] += res['fail']
                sheet_stats['errors'].extend(res['errors'])

    return sheet_stats

def generate_manifest(bag_dir: Path):
    manifest_path = bag_dir / "manifest-md5.txt"
    data_dir = bag_dir / "data"
    
    if not data_dir.exists():
        return

    with open(manifest_path, 'w', encoding='utf-8') as manifest_file:
        for f in data_dir.rglob("*"):
            if f.is_file() and not f.name.startswith("._"):
                checksum = calculate_md5(f)
                if checksum:
                    rel_path = f.relative_to(bag_dir)
                    manifest_file.write(f"{checksum}  {rel_path}\n")
    
    logger.info(f"Manifest created for package: {bag_dir.name}")

def main():
    args = parse_args()
    overall_stats = {'total': 0, 'success': 0, 'fail': 0, 'errors': []}
    
    for package in getattr(args, 'packages', []):
        base_path = Path(package)
        if not base_path.exists():
            logger.warning(f"Path does not exist: {base_path}")
            overall_stats['errors'].append(f"Target path not found: {base_path}")
            continue
            
        file_index = build_file_index(base_path)

        for excel_file in base_path.rglob("*.xlsx"):
            if not excel_file.name.startswith("._") and not excel_file.name.startswith("~$"):
                res = process_excel_file(excel_file, base_path, args.debug, file_index, args.workers, args.move)
                overall_stats['total'] += res['total']
                overall_stats['success'] += res['success']
                overall_stats['fail'] += res['fail']
                overall_stats['errors'].extend(res['errors'])

    title = f"Execution Summary{'  [DEBUG MODE]' if args.debug else ''}"
    summary = {
        "Total Files Targeted": overall_stats['total'],
        "Successful": f"{overall_stats['success']}/{overall_stats['total']}",
        "Failed": f"{overall_stats['fail']}/{overall_stats['total']}",
    }
    if overall_stats['errors']:
        summary["Errors"] = overall_stats['errors']
    else:
        summary["Status"] = "All operations completed without errors."
    print_standard_summary(title, summary, logger=logger)

if __name__ == "__main__":
    main()