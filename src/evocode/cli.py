import argparse

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="evocode",
        description="EvoCode: a code repair agent.",
    )
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()
