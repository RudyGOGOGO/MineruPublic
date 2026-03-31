#!/usr/bin/env python3
"""
Install missing dependencies for DeerFlow Mobile-Use Integration
"""

import subprocess
import sys
import os

def run_command(command, description):
    """Run a shell command and handle output"""
    print(f"🔧 {description}")
    print(f"   Running: {command}")
    
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        if result.returncode == 0:
            print("   ✅ Success!")
            if result.stdout.strip():
                print(f"   Output: {result.stdout.strip()}")
        else:
            print(f"   ❌ Error: {result.stderr.strip()}")
            return False
    except Exception as e:
        print(f"   ❌ Exception: {e}")
        return False
    
    return True

def main():
    print("🚀 DeerFlow Mobile-Use Integration - Dependency Installer")
    print("=" * 60)
    
    # Check if we're in a virtual environment
    if hasattr(sys, 'real_prefix') or (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix):
        print("✅ Virtual environment detected")
    else:
        print("⚠️  No virtual environment detected")
        print("   It's recommended to use a virtual environment")
    
    print(f"Python executable: {sys.executable}")
    
    # Install aiohttp
    success = run_command(f"{sys.executable} -m pip install aiohttp", "Installing aiohttp")
    
    if success:
        print("\n🎉 Dependencies installed successfully!")
        print("You can now run the verification script:")
        print("   python verify_integration_setup_fixed.py")
    else:
        print("\n❌ Failed to install dependencies")
        print("Please install manually:")
        print("   pip install aiohttp")

if __name__ == "__main__":
    main()