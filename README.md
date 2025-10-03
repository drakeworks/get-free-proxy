# Smart Proxy Management System

A high-performance proxy management system that automatically fetches, validates, and rotates proxies from multiple sources.

## 🛠️ Setup

1. **Create Virtual Environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## 🚀 Usage

### Basic Usage
```python
import asyncio
from get_proxies import initialize_smart_proxy_system, get_next_proxy

async def main():
    # Initialize the system
    result = await initialize_smart_proxy_system()
    
    if result['status'] == 'ready':
        # Get a proxy for LinkedIn (SSL-capable)
        proxy = get_next_proxy(site="linkedin")
        print(f"LinkedIn proxy: {proxy}")
        
        # Get a proxy for Indeed (HTTP)
        proxy = get_next_proxy(site="indeed")
        print(f"Indeed proxy: {proxy}")

asyncio.run(main())
```

### Command Line Usage
```bash
# Show help
python get-proxies.py --help

# Show available sources
python get-proxies.py --show-sources

# Fetch proxies with custom settings
python get-proxies.py --min-proxies 5 --timeout 5 --no-ssl

# Disable blocked sources
python get-proxies.py --disable-blocked
```

## 📡 Supported Proxy Sources

| Source | Description | Status |
|--------|-------------|--------|
| **monosans** | High success rate, good SSL coverage | ✅ Enabled |
| **proxyscrape_all** | All proxies (let our SSL filtering handle it) | ✅ Enabled |
| **proxyscrape_premium** | High-quality proxies | ✅ Enabled |
| **free_proxy_list** | Good SSL proxy ratio | ✅ Enabled |
| **freeproxy_world_https** | FreeProxy.world HTTPS proxies | ✅ Enabled |
| **freeproxy_world_http** | FreeProxy.world HTTP proxies | ✅ Enabled |
| **advanced_name** | Advanced.name proxy list | ✅ Enabled |
| **thespeedx** | Large volume, test last | ✅ Enabled |
| **spys_one** | High-quality proxies with SSL indicators | ⚠️ May be blocked |

## 🔧 Configuration Options

- `--min-proxies`: Minimum working proxies needed (default: 10)
- `--timeout`: Proxy validation timeout in seconds (default: 3)
- `--workers`: Number of concurrent validation threads (default: 10)
- `--ssl-only`: Only test and use SSL-capable proxies (default: True)
- `--no-ssl`: Disable SSL-only mode and test all proxies
- `--max-pages`: Maximum pages to fetch from paginated sources (default: 10)

## 📊 Features

- **Automatic Proxy Fetching**: Gets proxies from 9+ online sources
- **Smart Validation**: Tests proxies concurrently for better performance
- **Site-Specific Selection**: SSL proxies for LinkedIn, HTTP for Indeed
- **Persistent Storage**: Saves working proxies for future use
- **Rotation Management**: Efficient proxy rotation with dead proxy removal
- **Async Operations**: Non-blocking I/O for better performance

## 🔄 Backward Compatibility

The system maintains full backward compatibility with existing projects:
- All original functions still work
- Same command-line interface
- Same configuration options
- Same proxy file format
- Enhanced with async operations and better architecture
