#!/usr/bin/env python3
import requests
import json

def test_bridge_endpoints():
    base_url = "http://localhost:8888"
    
    print("🔍 Testing Fixed Mobile-Use Bridge Server...")
    
    # Test health endpoint
    try:
        response = requests.get(f"{base_url}/health", timeout=10)
        print(f"✅ Health Check: {response.status_code} - {response.json()}")
    except Exception as e:
        print(f"❌ Health Check Failed: {e}")
        return
    
    # Test device status endpoint (this was failing before)
    try:
        print("\n🔍 Testing device status endpoint...")
        response = requests.get(f"{base_url}/device/status", timeout=15)
        print(f"✅ Device Status: {response.status_code}")
        
        if response.status_code == 200:
            data = response.json()
            print(f"📱 Device Info: {json.dumps(data, indent=2)}")
            
            if data.get('devices'):
                print(f"\n🎉 SUCCESS! Found {len(data['devices'])} connected device(s)")
                for device in data['devices']:
                    print(f"   📱 {device['device_id']}: {device['width']}x{device['height']}")
            else:
                print("⚠️  No devices found")
        else:
            print(f"❌ Error: {response.text}")
            
    except Exception as e:
        print(f"❌ Device Status Failed: {e}")

if __name__ == "__main__":
    test_bridge_endpoints()