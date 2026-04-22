"""AutoPaper2Obsidian - Entry Script"""
import argparse
import sys
from src.config import *
from src.utils import setup_logging, ensure_dirs


def main():
    parser = argparse.ArgumentParser(description="AutoPaper2Obsidian - Automated Paper Processing Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, no file migration")
    parser.add_argument("--file", type=str, help="Process a single PDF file")
    args = parser.parse_args()
    
    logger = setup_logging("autopaper")
    ensure_dirs()
    
    logger.info("AutoPaper2Obsidian started")
    logger.info(f"Mode: {'dry-run' if args.dry_run else 'normal'}")
    
    # Run main pipeline
    from src.pipeline import run_pipeline
    run_pipeline(dry_run=args.dry_run, single_file=args.file)


if __name__ == "__main__":
    main()
