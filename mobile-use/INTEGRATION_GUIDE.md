# DeerFlow 2.0 + Mobile-Use Integration Guide

## Overview

This guide demonstrates a successful integration between **DeerFlow 2.0** (AI agent orchestration) and **Mobile-Use** (Android UI automation) systems. The integration enables natural language-driven mobile app testing and automation through a sandboxed yet powerful architecture.

## Architecture

### System Components

```
┌─────────────────┐    HTTP Bridge    ┌──────────────────────┐
│  DeerFlow 2.0   │◄─────────────────►│   Mobile-Use System  │
│  (Sandboxed)    │   localhost:8888   │   (Full Access)      │
│  /mnt/user-data/│                    │ /Users/.../mobile-use│
└─────────────────┘                    └──────────────────────┘
         │                                       │
         │                                       │
         ▼                                       ▼
   ┌─────────────┐                        ┌─────────────┐
   │ AI Planning │                        │Android Phone│
   │ & Analysis  │                        │ via ADB     │
   └─────────────┘                        └─────────────┘
```

### Key Features

1. **Natural Language Commands**: Use plain English to describe mobile automation tasks
2. **Intelligent Planning**: DeerFlow breaks down complex scenarios into executable steps
3. **Visual Understanding**: Mobile-Use provides GUI OCR + SoM perception for accurate UI interaction
4. **Memory & Learning**: System learns from automation traces to improve future performance
5. **Sandboxed Safety**: DeerFlow operates in restricted environment, ensuring system security

## Quick Start

### Prerequisites

1. **DeerFlow 2.0**: Running locally with HTTP API access
2. **Mobile-Use System**: Installed at `/Users/weizhang/workspace/models/mobile-use`
3. **Android Device**: Connected via ADB
4. **Python Environment**: Both systems using Python 3.12+

### Installation & Setup

1. **Copy Integration Files** to your mobile-use directory:
   ```bash
   cp mobile_use_bridge.py /Users/weizhang/workspace/models/mobile-use/
   cp deerflow_mobile_client.py /Users/weizhang/workspace/models/mobile-use/
   ```

2. **Start Mobile-Use Bridge Server**:
   ```bash
   cd /Users/weizhang/workspace/models/mobile-use
   python mobile_use_bridge.py
   ```

3. **Test Integration** from DeerFlow environment:
   ```bash
   python mobile_use_integration_test.py
   ```

## Usage Examples

### Basic Automation Task

```python
from mobile_automation_skill import MobileAutomationSkill, AutomationTask

async def simple_automation():
    async with MobileAutomationSkill() as skill:
        task = AutomationTask(
            goal="Open Calculator app and calculate 5 + 3",
            app_package="com.google.android.calculator"
        )
        
        result = await skill.execute_automation(task)
        if result.success:
            print(f"✅ Automation completed: {result.stdout}")
        else:
            print(f"❌ Failed: {result.error}")
```

### Complex Test Scenario

```python
scenario = {
    "name": "Settings Navigation Test",
    "steps": [
        {
            "goal": "Open Android Settings",
            "app_package": "com.android.settings"
        },
        {
            "goal": "Navigate to Display settings"
        },
        {
            "goal": "Check brightness settings"
        }
    ]
}

async with MobileAutomationSkill() as skill:
    results = await skill.run_test_scenario(scenario)
    print(f"Scenario completed: {results['passed_steps']}/{results['total_steps']} steps passed")
```

### App Exploration

```python
async with MobileAutomationSkill() as skill:
    exploration = await skill.explore_app("com.android.settings", duration_minutes=2)
    if exploration["success"]:
        print(f"Explored app, generated {len(exploration['traces'])} trace files")
```

## File Structure

```
/mnt/user-data/outputs/
├── mobile_use_bridge.py           # HTTP bridge server for mobile-use
├── deerflow_mobile_client.py      # DeerFlow client for bridge communication
├── mobile_automation_skill.py     # High-level automation skill
├── test_bridge_setup.py           # Mock bridge testing
├── mobile_use_integration_test.py # Full integration testing
└── INTEGRATION_GUIDE.md           # This documentation
```

## API Reference

### Bridge Endpoints

#### POST /automation
Execute a UI automation task.

**Request:**
```json
{
    "goal": "Open calculator and compute 2+2",
    "model_provider": "claude",
    "test_name": "calculator_test",
    "app_package": "com.google.android.calculator"
}
```

**Response:**
```json
{
    "success": true,
    "return_code": 0,
    "stdout": "Task completed successfully",
    "stderr": "",
    "traces": [
        {
            "filename": "trace_calculator_test_20260328.json",
            "content": "{...}"
        }
    ],
    "goal": "Open calculator and compute 2+2",
    "model_provider": "claude",
    "duration_seconds": 15.7
}
```

#### GET /device/status
Check Android device connectivity.

**Response:**
```json
{
    "success": true,
    "devices": [
        {"device_id": "emulator-5554", "status": "device"}
    ],
    "adb_output": "List of devices attached\\nemulator-5554\\tdevice",
    "device_count": 1
}
```

#### POST /explore
Explore an Android app to learn its interface.

**Request:**
```json
{
    "package_name": "com.android.settings",
    "duration_minutes": 2
}
```

### Skill Classes

#### `AutomationTask`
Represents a mobile automation task.

**Properties:**
- `goal`: Task description in natural language
- `app_package`: Target app package name (optional)
- `expected_outcome`: Expected result (optional)
- `model_provider`: AI model to use ("claude" default)
- `timeout_seconds`: Task timeout (120s default)

#### `MobileAutomationSkill`
Main skill class for mobile automation.

**Methods:**
- `check_device_status()`: Check device connectivity
- `execute_automation(task)`: Execute automation task
- `explore_app(package, duration)`: Explore app interface
- `run_test_scenario(scenario)`: Execute multi-step scenario

## Advanced Features

### Test Scenario Generation

The `MobileTestGenerator` class provides pre-built test scenarios:

```python
from mobile_automation_skill import MobileTestGenerator

generator = MobileTestGenerator()
settings_test = generator.generate_settings_test()
calculator_test = generator.generate_calculator_test()
```

### Trace Analysis

Each automation execution generates detailed traces:

```python
# Traces contain:
{
    "filename": "trace_example.json",
    "content": {
        "goal": "User goal",
        "steps": ["step1", "step2"],
        "screenshots": ["screen1.png"],
        "ui_elements": [...],
        "result": "success"
    }
}
```

### Error Handling

The integration includes comprehensive error handling:

- **Device disconnection**: Automatic retry with clear error messages
- **App crashes**: Graceful handling with fallback strategies
- **Network issues**: Timeout management and reconnection logic
- **Permission errors**: Clear diagnostics for ADB/app permissions

## Troubleshooting

### Common Issues

1. **Bridge Connection Failed**
   ```
   ❌ Bridge connectivity failed: Cannot connect to host localhost:8888
   ```
   **Solution**: Ensure mobile-use bridge server is running:
   ```bash
   cd /Users/weizhang/workspace/models/mobile-use
   python mobile_use_bridge.py
   ```

2. **No Android Devices Found**
   ```
   ❌ Device check failed: No devices found
   ```
   **Solution**: Check ADB connection:
   ```bash
   adb devices
   adb connect <device-ip>  # for wireless debugging
   ```

3. **Mobile-Use Module Not Found**
   ```
   ModuleNotFoundError: No module named 'mobile_use'
   ```
   **Solution**: Ensure bridge runs from mobile-use directory with proper environment:
   ```bash
   cd /Users/weizhang/workspace/models/mobile-use
   source .venv/bin/activate
   python mobile_use_bridge.py
   ```

### Debug Mode

Enable debug logging in bridge server:

```python
# In mobile_use_bridge.py, set:
DEBUG = True
```

### Performance Optimization

1. **Reduce timeout** for simple tasks:
   ```python
   task = AutomationTask(goal="...", timeout_seconds=30)
   ```

2. **Use app packages** for faster app targeting:
   ```python
   task = AutomationTask(
       goal="Open calculator", 
       app_package="com.google.android.calculator"
   )
   ```

3. **Batch operations** in scenarios:
   ```python
   # Group related actions in single goals
   task = AutomationTask(goal="Open settings, go to display, and check brightness")
   ```

## Security Considerations

1. **Sandboxed Execution**: DeerFlow operates in `/mnt/user-data/` sandbox
2. **Local Communication**: Bridge uses localhost-only HTTP (no external access)
3. **No Credential Storage**: No sensitive data stored in DeerFlow environment
4. **ADB Security**: Ensure ADB debugging is only enabled for trusted devices

## Future Enhancements

1. **Multi-Device Support**: Extend bridge to handle multiple Android devices
2. **iOS Integration**: Add support for iOS automation via Appium
3. **Test Recording**: Visual test recorder for creating scenarios
4. **Performance Analytics**: Automated performance testing and reporting
5. **Cloud Integration**: Optional cloud deployment with secure tunneling

## Conclusion

This integration demonstrates how to successfully bridge AI agent orchestration (DeerFlow) with specialized automation tools (Mobile-Use) while maintaining security and flexibility. The HTTP bridge pattern can be adapted for other integrations where sandboxed AI agents need to control external systems.

The combination provides:
- **Natural Language Interface** for complex mobile testing
- **Visual Understanding** of Android UIs
- **Automated Learning** from test execution
- **Scalable Architecture** for enterprise testing needs

For support, check the troubleshooting section or review the test scripts for working examples.