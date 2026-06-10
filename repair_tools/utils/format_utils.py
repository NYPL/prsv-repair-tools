import logging
from typing import Union, List, Dict, Any

def print_standard_summary(
    title: str, 
    stats: Union[Dict[str, Any], List[str]], 
    logger: logging.Logger = None, 
    print_to_console: bool = True
):
    """
    Prints or logs a clean, readable, and easily copyable summary table.
    
    Args:
        title (str): The title of the summary.
        stats (dict or list): Data to summarize. 
            If a dict: keys are left-aligned, values are printed. Lists and dicts are expanded as sub-items.
            If a list: items are printed as raw lines.
        logger (logging.Logger): Optional logger to write the summary to.
        print_to_console (bool): Whether to also print to stdout.
    """
    lines = [f"--- {title.upper()} ---"]
    
    if isinstance(stats, dict) and stats:
        flat_pairs = []
        for k, v in stats.items():
            if isinstance(v, list) and not v:
                flat_pairs.append((str(k), "None"))
            elif isinstance(v, list):
                flat_pairs.append((str(k), ""))
                for item in v:
                    flat_pairs.append(("", f"- {item}"))
            elif isinstance(v, dict):
                flat_pairs.append((str(k), ""))
                for sub_k, sub_v in v.items():
                    flat_pairs.append(("", f"- {sub_k}: {sub_v}"))
            else:
                flat_pairs.append((str(k), str(v)))
                
        keys = [k for k, v in flat_pairs if k]
        max_k_len = max([len(k) for k in keys]) if keys else 0
        
        for k, v in flat_pairs:
            if k:
                lines.append(f"{k:<{max_k_len}} | {v}")
            else:
                # Sub-item, indent
                lines.append(f"{' ':<{max_k_len}}   {v}")
    elif isinstance(stats, list) and stats:
        lines.extend(stats)
    elif not stats:
        lines.append("No data to report.")
        
    lines.append("-" * len(lines[0]))
    
    summary_text = "\n".join(lines)
    
    if print_to_console:
        print(f"\n{summary_text}\n")
        
    if logger:
        # Avoid double printing if using console handler
        if print_to_console and not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            # If logger only prints to console, and we already printed, do nothing
            pass
        else:
            for line in lines:
                logger.info(line)
