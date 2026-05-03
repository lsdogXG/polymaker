#!/usr/bin/env python3
"""
Simple HTTP server to serve the frontend on port 3000.
The frontend will connect to the backend API on port 8080.
"""

import http.server
import socketserver
import os
import sys

PORT = int(os.getenv("FRONTEND_PORT", "3000"))
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")


class CORSHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    """HTTP handler with CORS headers for development."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def end_headers(self):
        # Add CORS headers
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()


def main():
    # Change to frontend directory
    os.chdir(FRONTEND_DIR)

    with socketserver.TCPServer(("0.0.0.0", PORT), CORSHTTPRequestHandler) as httpd:
        print(f"=" * 60)
        print(f"  Frontend server running at:")
        print(f"  http://0.0.0.0:{PORT}")
        print(f"  http://localhost:{PORT}")
        print(f"")
        print(f"  Serving files from: {FRONTEND_DIR}")
        print(f"  Backend API expected at: port 8080")
        print(f"=" * 60)
        print(f"\nPress Ctrl+C to stop...")

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down frontend server...")
            sys.exit(0)


if __name__ == "__main__":
    main()
