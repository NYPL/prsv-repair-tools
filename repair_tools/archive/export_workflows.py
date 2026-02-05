import csv
import logging
import argparse
from pathlib import Path
from repair_tools.utils.preservica_search_parse import PreservicaAPI

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--startdate",
        "-sd",
        required=False,
        help="""WF from date eg. 2025-11-19""",
        )
    parser.add_argument(
        "--enddate",
        "-ed",
        required=False,
        help="""WF to date, eg. 2025-11-19""",
        )
    parser.add_argument(
        "--wfstate",
        type=str,
        default="Aborted,Active,Completed,Finished_Mixed_Outcome,Pending,Suspended,Unknown,Failed",
        help="Options: Aborted, Active, Completed, Finished_Mixed_Outcome, Pending, Suspended, Unknown, or Failed. Input as: Completed,Failed,Active [no spaces]",
        )
    parser.add_argument(
        "--credentials",
        type=str,
        required=True,
        help="which set of credentials to use",
        )
    parser.add_argument(
        "--saveto",
        type=Path,
        required=True,
        help="path to export csv file to",
        )

    return parser.parse_args()

def main():
    args = parse_args()
    
    # "2025-11-18T00:00:00.000Z"
    if args.startdate and args.enddate:
        from_date = f"{args.startdate}T00:00:00.000Z"
        to_date = f"{args.enddate}T00:00:00.000Z"
        output_file = Path(args.saveto / f"workflow_report_{args.startdate}_{args.enddate}.csv")
    elif args.startdate and not args.enddate:
        from_date = f"{args.startdate}T00:00:00.000Z"
        to_date = None
        output_file = Path(args.saveto / f"workflow_report_{args.startdate}_current.csv")
    else:
        from_date = None
        to_date = None
        output_file = Path(args.saveto / f"workflow_report_ALL.csv")

    headers = [
        'Id', 
        'CorrelationToken', 
        'Started', 
        'Finished', 
        'State', 
        'DisplayState', 
        'ArchivalProcessId', 
        'WorkflowGroupId', 
        'ProcessMonitorApiId', 
        'WorkflowContextId', 
        'WorkflowContextName', 
        'WorkflowDefinitionTextId', 
        'WorkflowDefinitionName', 
        'Creator', 
        'TopLevelDURecord'
    ]

    try:
        client = PreservicaAPI(args.credentials)
    except Exception as e:
        logger.error(f"Failed: {e}")
        return

    logger.info(f"Fetching workflows to {output_file}...")

    try:
        with open(output_file, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
            writer.writeheader()
            
            count = 0
            for workflow_instance in client.get_wf_instances(state=args.wfstate, from_date=from_date, to_date=to_date):
                writer.writerow(workflow_instance)
                count += 1
                
                if count % 100 == 0:
                    print(f"Written {count} rows...", end='\r')

        logger.info(f"Successfully wrote {count} workflow instances to {output_file}")

    except Exception as e:
        logger.error(f"Error during execution: {e}")

if __name__ == "__main__":
    main()
