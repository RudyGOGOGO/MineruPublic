#!/usr/bin/env python3
"""
Mobile-Use Bridge Server - Final Version with UIAutomator2 Device Detection
Fixes device detection issue by using UIAutomator2 directly like mobile-use does
"""

import asyncio
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import aiohttp
from aiohttp import web
# Using simple CORS middleware instead of aiohttp-cors

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('mobile_use_bridge')

class MobileUseBridge:
    def __init__(self, mobile_use_path: Path):
        self.mobile_use_path = Path(mobile_use_path)
        self.venv_python = self.mobile_use_path / ".venv" / "bin" / "python"
        self.ui_auto_script = self.mobile_use_path / "mineru" / "ui_auto" / "main.py"
        
        # Verify paths
        if not self.mobile_use_path.exists():
            raise ValueError(f"Mobile-use path not found: {mobile_use_path}")
        if not self.venv_python.exists():
            raise ValueError(f"Virtual environment not found: {self.venv_python}")
        if not self.ui_auto_script.exists():
            raise ValueError(f"Main script not found: {self.ui_auto_script}")
            
        logger.info(f"Initialized bridge with mobile-use at: {self.mobile_use_path}")

    async def execute_automation(self, goal: str, **kwargs) -> Dict[str, Any]:
        """Execute mobile automation command via ui-auto"""
        model_provider = kwargs.get('model_provider', 'claude')
        claude_model = kwargs.get('claude_model', 'claude-sonnet-4-6')
        test_name = kwargs.get('test_name', 'deerflow_test')
        enhanced_perception = kwargs.get('enhanced_perception', False)
        use_lessons = kwargs.get('use_lessons', False)
        output_description = kwargs.get('output_description', '')
        
        if not goal:
            return {"error": "Goal is required", "success": False}
            
        try:
            # Create temporary directory for traces and outputs
            with tempfile.TemporaryDirectory() as temp_dir:
                traces_path = Path(temp_dir) / "traces"
                traces_path.mkdir(exist_ok=True)
                
                lessons_dir = Path(temp_dir) / "lessons" if use_lessons else None
                if lessons_dir:
                    lessons_dir.mkdir(exist_ok=True)
                
                # Build command
                cmd = [
                    str(self.venv_python), 
                    str(self.ui_auto_script),
                    "main",  # Add the main subcommand
                    goal,
                    "--model-provider", model_provider,
                    "--claude-model", claude_model,
                    "--test-name", test_name,
                    "--traces-path", str(traces_path)
                ]
                
                if output_description:
                    cmd.extend(["--output-description", output_description])
                    
                if lessons_dir:
                    cmd.extend(["--lessons-dir", str(lessons_dir)])
                
                # Set environment for enhanced perception
                env = {}
                if enhanced_perception:
                    env["MOBILE_USE_PERCEPTION"] = "enhanced"
                
                # Execute command
                logger.info(f"Executing command: {' '.join(cmd)}")
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=self.mobile_use_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env if env else None
                )
                
                stdout_bytes, stderr_bytes = await process.communicate()
                stdout = stdout_bytes.decode('utf-8') if stdout_bytes else ""
                stderr = stderr_bytes.decode('utf-8') if stderr_bytes else ""
                
                # Collect trace files
                trace_files = []
                if traces_path.exists():
                    for trace_file in traces_path.rglob("*"):
                        if trace_file.is_file():
                            try:
                                if trace_file.suffix.lower() in ['.png', '.jpg', '.jpeg']:
                                    # For images, just record the path and size
                                    trace_files.append({
                                        "type": "image",
                                        "path": str(trace_file),
                                        "size": trace_file.stat().st_size
                                    })
                                else:
                                    # For text files, include content
                                    content = trace_file.read_text(encoding='utf-8', errors='ignore')
                                    trace_files.append({
                                        "type": "text",
                                        "path": str(trace_file),
                                        "content": content
                                    })
                            except Exception as e:
                                logger.warning(f"Could not read trace file {trace_file}: {e}")
                
                result = {
                    "success": process.returncode == 0,
                    "return_code": process.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                    "traces": trace_files,
                    "goal": goal,
                    "model_provider": model_provider,
                    "claude_model": claude_model,
                    "enhanced_perception": enhanced_perception
                }
                
                logger.info(f"Command completed with return code: {process.returncode}")
                return result
                
        except Exception as e:
            logger.error(f"Error executing automation: {e}")
            return {
                "error": str(e),
                "success": False,
                "goal": goal
            }

    async def check_device_status(self) -> Dict[str, Any]:
        """Check Android device connectivity using UIAutomator2 like mobile-use does"""
        try:
            # First check ADB devices
            adb_process = await asyncio.create_subprocess_exec(
                "adb", "devices",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            adb_stdout, adb_stderr = await adb_process.communicate()
            
            adb_devices = []
            if adb_process.returncode == 0:
                lines = adb_stdout.decode('utf-8').strip().split('\n')[1:]  # Skip header
                for line in lines:
                    if line.strip() and '\tdevice' in line:
                        device_id = line.split('\t')[0].strip()
                        adb_devices.append(device_id)
                        
            logger.info(f"ADB devices found: {adb_devices}")
            
            # Now check UIAutomator2 connectivity using mobile-use approach
            ui_devices = []
            if adb_devices:
                # Create a simple test script to check UIAutomator2 connection
                test_script = f'''
import sys
sys.path.insert(0, "{self.mobile_use_path}")

try:
    import uiautomator2 as u2
    devices = []
    for device_id in {adb_devices}:
        try:
            d = u2.connect(device_id)
            # Test actual connection
            info = d.info
            devices.append({{
                "device_id": device_id,
                "connected": True,
                "width": info.get("displayWidth", 0),
                "height": info.get("displayHeight", 0),
                "platform": info.get("platform", "android"),
                "version": info.get("version", "unknown")
            }})
        except Exception as e:
            devices.append({{
                "device_id": device_id,
                "connected": False,
                "error": str(e)
            }})
    
    import json
    print(json.dumps(devices))
    
except Exception as e:
    print(json.dumps([{{"error": str(e), "connected": False}}]))
'''
                
                # Execute the test script
                test_process = await asyncio.create_subprocess_exec(
                    str(self.venv_python), "-c", test_script,
                    cwd=self.mobile_use_path,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE
                )
                
                test_stdout, test_stderr = await test_process.communicate()
                
                if test_process.returncode == 0:
                    try:
                        ui_devices = json.loads(test_stdout.decode('utf-8').strip())
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse UIAutomator2 test result: {e}")
                        logger.error(f"stdout: {test_stdout.decode('utf-8')}")
                        logger.error(f"stderr: {test_stderr.decode('utf-8')}")
                else:
                    logger.error(f"UIAutomator2 test failed: {test_stderr.decode('utf-8')}")
            
            # Count connected devices
            connected_count = len([d for d in ui_devices if d.get('connected', False)])
            
            result = {
                "adb_available": adb_process.returncode == 0,
                "adb_devices": adb_devices,
                "ui_devices": ui_devices,
                "connected_devices": connected_count,
                "status": "healthy" if connected_count > 0 else "no_devices"
            }
            
            logger.info(f"Device status: {connected_count} devices connected")
            return result
            
        except Exception as e:
            logger.error(f"Error checking device status: {e}")
            return {
                "error": str(e),
                "adb_available": False,
                "connected_devices": 0,
                "status": "error"
            }

# Global bridge instance
_bridge = None

def get_bridge():
    global _bridge
    if _bridge is None:
        # Auto-detect mobile-use path
        mobile_use_path = Path("/Users/weizhang/workspace/models/mobile-use")
        if not mobile_use_path.exists():
            # Fallback paths
            for path in [
                Path.home() / "workspace" / "models" / "mobile-use",
                Path("/usr/local/mobile-use"),
                Path("./mobile-use")
            ]:
                if path.exists():
                    mobile_use_path = path
                    break
        
        _bridge = MobileUseBridge(mobile_use_path)
    return _bridge

# CORS Middleware - Fixed version
@web.middleware
async def cors_middleware(request, handler):
    """CORS middleware to handle cross-origin requests"""
    # Handle preflight requests
    if request.method == 'OPTIONS':
        response = web.Response()
    else:
        response = await handler(request)
    
    # Add CORS headers
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Max-Age'] = '86400'
    
    return response

# Route Handlers
async def health_check(request):
    """Health check endpoint"""
    try:
        bridge = get_bridge()
        return web.json_response({
            "status": "healthy",
            "service": "mobile-use-bridge",
            "mobile_use_path": str(bridge.mobile_use_path),
            "ui_auto_available": bridge.ui_auto_script.exists(),
            "venv_available": bridge.venv_python.exists()
        })
    except Exception as e:
        return web.json_response({
            "status": "error",
            "error": str(e)
        }, status=500)

async def device_status(request):
    """Get device status"""
    try:
        bridge = get_bridge()
        status = await bridge.check_device_status()
        return web.json_response(status)
    except Exception as e:
        return web.json_response({
            "error": str(e),
            "connected_devices": 0
        }, status=500)

async def execute_automation(request):
    """Execute automation command"""
    try:
        bridge = get_bridge()
        data = await request.json()
        result = await bridge.execute_automation(**data)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({
            "error": str(e),
            "success": False
        }, status=500)

def create_app():
    """Create the web application"""
    app = web.Application(middlewares=[cors_middleware])
    
    # Add routes
    app.router.add_get('/health', health_check)
    app.router.add_get('/device/status', device_status)
    app.router.add_post('/automation', execute_automation)
    
    return app

async def main():
    """Main server function"""
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, 'localhost', 8888)
    await site.start()
    
    logger.info("Mobile-Use Bridge Server started on http://localhost:8888")
    logger.info("Available endpoints:")
    logger.info("  GET  /health - Health check")
    logger.info("  GET  /device/status - Device status")
    logger.info("  POST /automation - Execute automation")
    
    try:
        # Keep the server running
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Server shutting down...")
    finally:
        await runner.cleanup()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped")