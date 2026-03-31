#!/usr/bin/env python3
"""
Quick Device Detection Verification Script
Tests that the bridge server can properly detect Android devices
"""

import asyncio
import aiohttp
import json

async def test_device_detection():
    """Test device detection through the bridge API"""
    print("🔍 Testing device detection via bridge API...")
    print("=" * 50)
    
    base_url = "http://localhost:8888"
    
    async with aiohttp.ClientSession() as session:
        try:
            # Test health endpoint
            print("1️⃣ Testing health endpoint...")
            async with session.get(f"{base_url}/health") as response:
                if response.status == 200:
                    health_data = await response.json()
                    print(f"✅ Health check passed: {health_data}")
                else:
                    print(f"❌ Health check failed: {response.status}")
                    return False
            
            print()
            
            # Test device status endpoint
            print("2️⃣ Testing device detection...")
            async with session.get(f"{base_url}/device/status") as response:
                if response.status == 200:
                    device_data = await response.json()
                    print(f"📱 Device status response:")
                    print(json.dumps(device_data, indent=2))
                    
                    if device_data.get("status") == "connected" and device_data.get("devices"):
                        devices = device_data["devices"]
                        print(f"\n🎉 SUCCESS: {len(devices)} device(s) detected!")
                        
                        for i, device in enumerate(devices, 1):
                            device_id = device.get("device_id", "Unknown")
                            screen_size = device.get("screen_size", "Unknown")
                            print(f"   Device {i}: {device_id} ({screen_size})")
                        
                        return True
                    else:
                        print("❌ No devices detected or disconnected status")
                        print("   Make sure your Android device is:")
                        print("   - Connected via USB")
                        print("   - USB debugging enabled")
                        print("   - Authorized for this computer")
                        return False
                else:
                    print(f"❌ Device status request failed: {response.status}")
                    error_text = await response.text()
                    print(f"   Error: {error_text}")
                    return False
            
        except aiohttp.ClientConnectorError:
            print("❌ Cannot connect to bridge server")
            print("   Make sure the bridge server is running on localhost:8888")
            return False
        except Exception as e:
            print(f"❌ Unexpected error: {e}")
            return False

async def main():
    """Main test function"""
    print("🚀 Mobile-Use Bridge Device Detection Test")
    print("=" * 50)
    
    success = await test_device_detection()
    
    print("\n" + "=" * 50)
    if success:
        print("🎉 Device detection working properly!")
        print("✨ Your bridge is ready for real mobile automation!")
        print("\nNext steps:")
        print("1. Use DeerFlow mobile automation skill")
        print("2. Send natural language automation commands")
        print("3. Watch real device automation happen!")
    else:
        print("⚠️ Device detection needs attention")
        print("\nTroubleshooting:")
        print("1. Check that bridge server is running (localhost:8888)")
        print("2. Verify Android device USB debugging is enabled")
        print("3. Ensure device is authorized for this computer")
        print("4. Try disconnecting and reconnecting the device")
    
    return 0 if success else 1

if __name__ == "__main__":
    try:
        exit_code = asyncio.run(main())
        exit(exit_code)
    except KeyboardInterrupt:
        print("\n👋 Test cancelled")
        exit(1)