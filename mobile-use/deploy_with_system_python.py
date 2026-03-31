#!/usr/bin/env python3
"""
Deploy Mobile-Use Bridge with System Python
Alternative deployment using system Python instead of mobile-use venv
"""

import subprocess
import sys
import time
import requests
from pathlib import Path
import shutil

class SystemPythonDeployer:
    def __init__(self):
        self.mobile_use_path = Path("/Users/weizhang/workspace/models/mobile-use")
        self.outputs_path = Path("/Users/weizhang/workspace/models/agentic-system/deer-flow/backend/.deer-flow/threads/e60de3a1-d129-4829-8edd-7854347cce44/user-data/outputs")
        self.bridge_script = "mobile_use_bridge_final.py"
        
    def run_command(self, cmd, cwd=None, check=True, timeout=10):
        """Run a shell command"""
        print(f"🔧 Running: {' '.join(cmd)}")
        try:
            result = subprocess.run(
                cmd, 
                cwd=cwd or self.mobile_use_path,
                capture_output=True, 
                text=True, 
                check=check,
                timeout=timeout
            )
            if result.stdout:
                print(f"✅ Output: {result.stdout.strip()}")
            return result
        except subprocess.CalledProcessError as e:
            print(f"❌ Error: {e}")
            if e.stderr:
                print(f"   Stderr: {e.stderr.strip()}")
            return e
        except subprocess.TimeoutExpired:
            print(f"⏰ Command timed out after {timeout} seconds")
            return None
    
    def check_system_python_deps(self):
        """Check if system Python has required dependencies"""
        print("🔍 Checking system Python dependencies...")
        
        # You already showed aiohttp is installed in system Python
        result = self.run_command(["python3.11", "-c", "import aiohttp; print(f'aiohttp {aiohttp.__version__}')"], check=False)
        if isinstance(result, subprocess.CalledProcessError):
            print("❌ aiohttp not found in system Python")
            return False
        
        # Check for uiautomator2 (might need to install)
        result = self.run_command(["python3.11", "-c", "import uiautomator2; print(f'uiautomator2 {uiautomator2.__version__}')"], check=False)
        if isinstance(result, subprocess.CalledProcessError):
            print("⚠️ uiautomator2 not found, installing...")
            install_result = self.run_command(["pip3.11", "install", "uiautomator2"], check=False)
            if isinstance(install_result, subprocess.CalledProcessError):
                print("❌ Failed to install uiautomator2")
                return False
        
        print("✅ System Python dependencies are ready")
        return True
    
    def kill_existing_servers(self):
        """Kill any existing servers on port 8888"""
        print("🔍 Checking for existing servers on port 8888...")
        result = self.run_command(["lsof", "-ti:8888"], check=False)
        
        if result and hasattr(result, 'stdout') and result.stdout.strip():
            pids = result.stdout.strip().split('\n')
            for pid in pids:
                if pid:
                    print(f"🔪 Killing process {pid}")
                    self.run_command(["kill", "-9", pid], check=False)
        else:
            print("✅ No existing servers found")
    
    def copy_bridge_script(self):
        """Copy bridge script to mobile-use directory"""
        print("📄 Copying bridge script...")
        
        source = self.outputs_path / self.bridge_script
        destination = self.mobile_use_path / self.bridge_script
        
        if not source.exists():
            print(f"❌ Source script not found: {source}")
            return False
        
        try:
            shutil.copy2(source, destination)
            print(f"✅ Bridge script copied to {destination}")
            return True
        except Exception as e:
            print(f"❌ Failed to copy script: {e}")
            return False
    
    def modify_bridge_for_system_python(self):
        """Modify bridge script to work with system Python paths"""
        print("🔧 Modifying bridge script for system Python...")
        
        bridge_path = self.mobile_use_path / self.bridge_script
        
        try:
            with open(bridge_path, 'r') as f:
                content = f.read()
            
            # Update the mobile_use_path in the script
            updated_content = content.replace(
                'self.mobile_use_path = "/path/to/mobile-use"',
                f'self.mobile_use_path = "{self.mobile_use_path}"'
            )
            
            # Make sure it uses the correct Python path
            if 'self.python_path' in updated_content:
                updated_content = updated_content.replace(
                    'self.python_path = self.mobile_use_path / ".venv" / "bin" / "python"',
                    'self.python_path = "/opt/homebrew/bin/python3.11"'
                )
            
            with open(bridge_path, 'w') as f:
                f.write(updated_content)
                
            print("✅ Bridge script updated for system Python")
            return True
        except Exception as e:
            print(f"❌ Failed to modify bridge script: {e}")
            return False
    
    def start_bridge_server(self):
        """Start the bridge server"""
        print("🚀 Starting bridge server...")
        
        bridge_path = self.mobile_use_path / self.bridge_script
        
        # Start server in background
        try:
            process = subprocess.Popen(
                ["python3.11", str(bridge_path)],
                cwd=self.mobile_use_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            print(f"🔄 Bridge server starting... (PID: {process.pid})")
            
            # Wait a bit for server to start
            time.sleep(3)
            
            # Check if process is still running
            if process.poll() is None:
                print("✅ Bridge server started successfully")
                return process
            else:
                stdout, stderr = process.communicate()
                print(f"❌ Bridge server failed to start")
                if stdout:
                    print(f"   Stdout: {stdout}")
                if stderr:
                    print(f"   Stderr: {stderr}")
                return None
        except Exception as e:
            print(f"❌ Failed to start bridge server: {e}")
            return None
    
    def test_bridge_endpoints(self):
        """Test bridge server endpoints"""
        print("🔍 Testing bridge endpoints...")
        
        base_url = "http://localhost:8888"
        
        # Test health endpoint
        try:
            response = requests.get(f"{base_url}/health", timeout=5)
            if response.status_code == 200:
                print("✅ Health endpoint working")
            else:
                print(f"❌ Health endpoint failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Health endpoint error: {e}")
            return False
        
        # Test device status endpoint
        try:
            response = requests.get(f"{base_url}/device/status", timeout=10)
            if response.status_code == 200:
                data = response.json()
                print(f"✅ Device status: {data}")
                return True
            else:
                print(f"❌ Device status failed: {response.status_code}")
                return False
        except Exception as e:
            print(f"❌ Device status error: {e}")
            return False

def main():
    print("🚀 Mobile-Use Bridge System Python Deployment")
    print("=" * 60)
    
    deployer = SystemPythonDeployer()
    
    # Check system Python dependencies
    if not deployer.check_system_python_deps():
        print("❌ System Python dependencies not ready")
        return 1
    
    # Kill existing servers
    deployer.kill_existing_servers()
    
    # Copy bridge script
    if not deployer.copy_bridge_script():
        print("❌ Failed to copy bridge script")
        return 1
    
    # Modify for system Python
    if not deployer.modify_bridge_for_system_python():
        print("❌ Failed to modify bridge script")
        return 1
    
    # Start bridge server
    server_process = deployer.start_bridge_server()
    if not server_process:
        print("❌ Failed to start bridge server")
        return 1
    
    # Test endpoints
    if not deployer.test_bridge_endpoints():
        print("❌ Bridge endpoints not responding correctly")
        if server_process:
            server_process.terminate()
        return 1
    
    print("\n" + "=" * 60)
    print("🎉 Bridge server deployed successfully!")
    print("🌐 Server running at: http://localhost:8888")
    print("📱 Device integration ready for testing")
    print(f"🔧 Server PID: {server_process.pid}")
    print("\n📋 Next steps:")
    print("1. Test DeerFlow mobile automation commands")
    print("2. Server logs available in terminal")
    print("3. Use Ctrl+C to stop server when done")
    
    # Keep server running
    try:
        server_process.wait()
    except KeyboardInterrupt:
        print("\n👋 Shutting down server...")
        server_process.terminate()
        server_process.wait()
    
    return 0

if __name__ == "__main__":
    try:
        exit_code = main()
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n👋 Operation cancelled")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)