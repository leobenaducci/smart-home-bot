#!/usr/bin/env python3
"""
Main entry point for the Camera Video Analysis System
"""

import sys
import os
import argparse
import logging

from web_server import app, socketio
from settings_manager import SettingsManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='Camera Video Analysis System')
    parser.add_argument('--host', default='0.0.0.0', help='Host to run the server on')
    parser.add_argument('--port', type=int, default=5000, help='Port to run the server on')
    parser.add_argument('--debug', action='store_true', help='Run in debug mode')
    
    args = parser.parse_args()
    
    logger.info(f"Starting Camera Video Analysis System on {args.host}:{args.port}")
    logger.info("Make sure to install dependencies with: pip install -r requirements.txt")
    
    # Seed a default admin password only on first run. Doing this unconditionally
    # reset the password to 'admin' on every restart and silently discarded any
    # password restored from backup.
    settings_manager = SettingsManager()
    if settings_manager.get_admin_config() is None:
        settings_manager.initialize_default_admin('admin')
        logger.warning("No admin password found — seeded the default 'admin'. "
                       "Change it via POST /api/change_password.")
    else:
        logger.info("Admin password loaded from config")
    
    try:
        # Run the Flask-SocketIO app
        socketio.run(app,
                    host=args.host,
                    port=args.port,
                    debug=args.debug,
                    allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        logger.info("Shutting down server...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()