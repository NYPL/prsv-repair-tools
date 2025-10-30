import json
import argparse
from pathlib import Path

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--index",
        "-i",
        required=True,
        help="""Index to search in.""",
        )
    parser.add_argument(
        "--basepath",
        "-b",
        required=False,
        help="""Bath path to prepend to results.""",
        )
    parser.add_argument(
        "--searchterm",
        "-s",
        required=True,
        help="""The term to search for in the index.""",
        )
    return parser.parse_args()

def search_tree_for_key(json_obj, query, current_path=None):
    if current_path is None:
        current_path = []

    if not isinstance(json_obj, dict):
        return []

    found_paths = []
    normalized_query = query.lower()

    for key, value in json_obj.items():
        normalized_key = key.lower()
        
        if normalized_query in normalized_key:
            path_str = "/".join(current_path + [key])
            found_paths.append(path_str)

        if isinstance(value, dict):
            new_path_prefix = current_path + [key]
            found_paths.extend(search_tree_for_key(value, query, new_path_prefix))
    
    return found_paths

def main():
    args = parse_args()

    index_file = args.index
    index_path = Path(index_file)
    search_term = args.searchterm

    # interchangeable use of args and index_file parent (depending on where the index is located)
    base_dir = Path(args.basepath)

    # try:
    #     base_dir = Path(index_file).resolve().parent
    # except Exception as e:
    #     print(f"Warning: Could not resolve full path. Using relative parent. Error: {e}")
    #     base_dir = Path(index_file).parent
    
    try:
        with open(index_file, 'r', encoding='utf-8') as f:
            index_data = json.load(f)
    except FileNotFoundError:
        print(f"Error: Index file not found at '{index_file}'")
        return
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from '{index_file}'")
        return
    except Exception as e:
        print(f"Error occurred while reading the file: {e}")
        return

    results = search_tree_for_key(index_data, search_term)

    if results:
        print(f"Found {len(results)} matches for '{search_term}':")
        if args.basepath:
            print(f"Prepending base path: {base_dir}\n")
            for relative_path in results:
                full_path = base_dir / relative_path
                print(full_path)
        else:
            for path in results:
                print(path)
    else:
        print(f"No matches found for '{search_term}'.")


if __name__ == "__main__":
    main()

