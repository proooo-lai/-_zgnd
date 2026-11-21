"""Local desktop entry point; visit http://127.0.0.1:8765."""
import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run("storybook.server:app", host=args.host, port=args.port, log_level="info")
