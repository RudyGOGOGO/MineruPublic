#!/usr/bin/env python3
"""
Deploy Fixed Mobile-Use Bridge with UIAutomator2 Device Detection
Comprehensive deployment script that handles server restart and validation
"""

import asyncio
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

class BridgeDeployer:
    def __init__(self):
        self.mobile_use_path = Path("/Users/weizhang/workspace/models/mobile-use")
        self.bridge_script_name = "mobile_use_bridge_final.py"
        self.port = 8888
        
    def check_prerequisites(self):
        """Check if all prerequisites are met"""
        print("🔍 Checking prerequisites...")
        
        # Check mobile-use path
        if not self.mobile_use_path.exists():
            print(f"❌ Mobile-use path not found: {self.mobile_use_path}")
            return False
        print(f"✅ Mobile-use path found: {self.mobile_use_path}")
        
        # Check virtual environment
        venv_python = self.mobile_use_path / ".venv" / "bin" / "python"
        if not venv_python.exists():
            print(f"❌ Virtual environment not found: {venv_python}")
            return False
        print(f"✅ Virtual environment found: {venv_python}")
        
        # Check ui-auto script
        ui_auto_script = self.mobile_use_path / "mineru" / "ui_auto" / "main.py"
        if not ui_auto_script.exists():
            print(f"❌ UI-auto script not found: {ui_auto_script}")
            return False
        print(f"✅ UI-auto script found: {ui_auto_script}")
        
        # Check aiohttp in mobile-use venv
        try:
            result = subprocess.run([
                str(venv_python), "-c", "import aiohttp; print(aiohttp.__version__)"
            ], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                print(f"✅ aiohttp available: {result.stdout.strip()}")
            else:
                print(f"❌ aiohttp not available in mobile-use venv")
                print("Installing aiohttp...")
                install_result = subprocess.run([
                    str(venv_python), "-m", "pip", "install", "aiohttp", "aiohttp-cors"
                ], capture_output=True, text=True, timeout=60)
                if install_result.returncode == 0:
                    print("✅ aiohttp installed successfully")
                else:
                    print(f"❌ Failed to install aiohttp: {install_result.stderr}")
                    return False
        except Exception as e:
            print(f"❌ Error checking aiohttp: {e}")
            return False
            
        return True
    
    def stop_existing_server(self):
        """Stop any existing bridge server on the port"""
        print(f"🛑 Stopping existing servers on port {self.port}...")
        
        try:
            # Find processes using the port
            result = subprocess.run([
                "lsof", "-ti", f":{self.port}"
            ], capture_output=True, text=True)
            
            if result.returncode == 0 and result.stdout.strip():
                pids = result.stdout.strip().split('\n')
                for pid in pids:
                    try:
                        pid = int(pid.strip())
                        print(f"🔪 Killing process {pid}...")
                        os.kill(pid, signal.SIGTERM)
                        time.sleep(2)
                        # Force kill if still running
                        try:
                            os.kill(pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass  # Process already dead
                    except (ValueError, ProcessLookupError):
                        pass
                print("✅ Stopped existing servers")
            else:
                print("✅ No existing servers found")
                
        except Exception as e:
            print(f"⚠️ Warning: Could not stop existing servers: {e}")
    
    def copy_bridge_script(self):
        """Copy the fixed bridge script to mobile-use directory"""
        print("📋 Copying fixed bridge script...")
        
        source_path = Path("/mnt/user-data/outputs/mobile_use_bridge_final.py")
        if not source_path.exists():
            print(f"❌ Source bridge script not found: {source_path}")
            return False
            
        dest_path = self.mobile_use_path / self.bridge_script_name
        try:
            shutil.copy2(source_path, dest_path)
            dest_path.chmod(0o755)
            print(f"✅ Copied bridge script to: {dest_path}")
            return True
        except Exception as e:
            print(f"❌ Failed to copy bridge script: {e}")
            return False
    
    async def start_bridge_server(self):
        """Start the fixed bridge server"""
        print("🚀 Starting fixed bridge server...")
        
        bridge_script = self.mobile_use_path / self.bridge_script_name
        venv_python = self.mobile_use_path / ".venv" / "bin" / "python"
        
        try:
            # Start server in background
            process = await asyncio.create_subprocess_exec(
                str(venv_python), str(bridge_script),
                cwd=self.mobile_use_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            # Give server time to start
            await asyncio.sleep(3)
            
            # Check if server is still running
            if process.returncode is None:
                print("✅ Bridge server started successfully")
                return process
            else:
                stdout, stderr = await process.communicate()
                print(f"❌ Server failed to start")
                print(f"stdout: {stdout.decode()}")
                print(f"stderr: {stderr.decode()}")
                return None
                
        except Exception as e:
            print(f"❌ Failed to start server: {e}")
            return None
    
    async def validate_deployment(self):
        """Validate the deployment by running device detection test"""
        print("🧪 Validating deployment...")
        
        # Copy test script
        test_source = Path("/mnt/user-data/outputs/test_device_detection.py")
        test_dest = self.mobile_use_path / "test_device_detection.py"
        
        try:
            shutil.copy2(test_source, test_dest)
            test_dest.chmod(0o755)
        except Exception as e:
            print(f"⚠️ Could not copy test script: {e}")
            return False
        
        # Run test
        venv_python = self.mobile_use_path / ".venv" / "bin" / "python"
        try:
            process = await asyncio.create_subprocess_exec(
                str(venv_python), str(test_dest),
                cwd=self.mobile_use_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            print("📊 Validation test results:")
            print("-" * 40)
            print(stdout.decode())
            if stderr:
                print("⚠️ Warnings/Errors:")
                print(stderr.decode())
            print("-" * 40)
            
            return process.returncode == 0
            
        except Exception as e:
            print(f"❌ Validation failed: {e}")
            return False

async def main():
    """Main deployment function"""
    deployer = BridgeDeployer()
    
    print("🚀 Mobile-Use Bridge Fixed Deployment")
    print("=" * 50)
    
    # Check prerequisites
    if not deployer.check_prerequisites():
        print("❌ Prerequisites not met. Please fix issues and try again.")
        return 1
    
    print()
    
    # Stop existing server
    deployer.stop_existing_server()
    print()
    
    # Copy bridge script
    if not deployer.copy_bridge_script():
        print("❌ Failed to copy bridge script.")
        return 1
    
    print()
    
    # Start server
    server_process = await deployer.start_bridge_server()
    if not server_process:
        print("❌ Failed to start bridge server.")
        return 1
    
    print()
    
    # Validate deployment
    validation_success = await deployer.validate_deployment()
    
    if validation_success:
        print("\n🎉 Deployment successful!")
        print("✨ Your mobile automation bridge is ready with proper device detection!")
        print(f"🌐 Server running at: http://localhost:{deployer.port}")
        print("\nEndpoints:")
        print("  GET  /health - Health check")
        print("  GET  /device/status - Device status with UIAutomator2 detection")
        print("  POST /automation - Execute automation")
        print("\nTo test real automation, use your DeerFlow mobile automation skill!")
    else:
        print("\n⚠️ Deployment completed but validation had issues.")
        print("The server is running, but device detection may need attention.")
        print("Check that your Android device is properly connected.")
    
    print(f"\n📝 Bridge server running as PID: {server_process.pid}")
    print("Press Ctrl+C to stop the server")
    
    try:
        # Keep script running to monitor server
        await server_process.wait()
    except KeyboardInterrupt:
        print("\n🛑 Stopping server...")
        server_process.terminate()
        await server_process.wait()
        print("✅ Server stopped")
    
    return 0

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n👋 Deployment cancelled")
        sys.exit(1)