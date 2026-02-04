import sys
import json
import argparse
from pathlib import Path

def create_directory_index_json(root_dir: Path):
    dir_name = root_dir.name
    output_filename = f"{dir_name}_index.json"
    
    output_filepath = root_dir / output_filename
    
    dir_tree = {}
    
    path_to_node_map = {root_dir: dir_tree}

    dir_stack = [root_dir]

    while dir_stack:
        current_dir_path = dir_stack.pop()
        
        current_node = path_to_node_map.get(current_dir_path)

        if current_node is None:
            print(f"WARNING: Could not find node for path {current_dir_path}. Skipping.")
            continue

        local_dirnames = []
        local_filenames = []
        
        try:
            for p in current_dir_path.iterdir():
                if p.name.startswith('.'):
                    continue
                
                if p.is_dir():
                    local_dirnames.append(p.name)
                elif p.is_file():
                    if p.name != output_filename:
                        local_filenames.append(p.name)

        except PermissionError:
            print(f"WARNING: Permission denied for directory {current_dir_path}. Skipping.")
            continue
        except FileNotFoundError:
            print(f"WARNING: Directory {current_dir_path} not found. Skipping.")
            continue
            
        local_dirnames.sort()
        local_filenames.sort()
        
        for f in local_filenames:
            current_node[f] = None

        for d in local_dirnames:
            new_dir_node = {}
            current_node[d] = new_dir_node
            
            new_dir_path = current_dir_path / d
            path_to_node_map[new_dir_path] = new_dir_node
            
            dir_stack.append(new_dir_path)
    
    try:
        with output_filepath.open('w', encoding='utf-8') as f:
            json.dump(dir_tree, f, indent=2, ensure_ascii=False)
        print(f"Successfully created JSON index at: {output_filepath}")
    except IOError as e:
        print(f"ERROR: Could not write file to {output_filepath}: {e}")
    except TypeError as e:
        print(f"ERROR: Could not serialize data to JSON: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

def main():
    parser = argparse.ArgumentParser(
        description="Generates a JSON index of a directory structure."
    )
    
    parser.add_argument(
        "--path",
        metavar="directory_path",
        type=Path,
        help="The path to the directory you want to index."
    )
    
    args = parser.parse_args()
    
    target_dir = args.path
    
    if not target_dir.is_dir():
        print(f"ERROR: Path '{target_dir}' is not a valid directory.")
        sys.exit(1)
        
    abs_target_dir = target_dir.resolve()
    
    print(f"Scanning directory: {abs_target_dir}...")
    create_directory_index_json(abs_target_dir)

if __name__ == "__main__":
    main()

