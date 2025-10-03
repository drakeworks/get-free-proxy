#!/usr/bin/env python3
"""
Smart Proxy Management System - Refactored Architecture
- Modular design with focused components
- Async operations for better concurrency
- Loads saved working proxies first
- Quick validation of existing proxies
- Full proxy fetching only when needed
- User choice for proxy vs no-proxy operation
"""

import json
import os
import time
import asyncio
import aiohttp
import requests
from typing import List, Dict, Optional, Set, Tuple
import logging
from bs4 import BeautifulSoup
from dataclasses import dataclass
from abc import ABC, abstractmethod
import random
from collections import deque

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@dataclass
class ProxyConfig:
    """Configuration for proxy management"""
    min_working_proxies: int = 3
    validation_timeout: int = 3
    max_workers: int = 10
    proxy_cache_file: str = "saved-proxies.json"
    ssl_only: bool = True
    verbose: bool = False
    max_pages_per_source: int = 10

class ProxyStorage:
    """Handles proxy persistence and loading"""
    
    def __init__(self, cache_file: str):
        self.cache_file = cache_file
    
    async def load_saved_proxies(self) -> Tuple[List[str], List[str]]:
        """Load previously saved working proxies from file with separate pool support"""
        try:
            if os.path.exists(self.cache_file):
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    
                    # Check if we have the new format with separate pools
                    if 'ssl_proxies' in data and 'http_proxies' in data:
                        ssl_proxies = data.get('ssl_proxies', [])
                        http_proxies = data.get('http_proxies', [])
                        
                        # Deduplicate when loading
                        ssl_proxies = self._deduplicate_list(ssl_proxies)
                        http_proxies = self._deduplicate_list(http_proxies)
                        
                        logger.info(f"Loaded {len(ssl_proxies)} SSL proxies and {len(http_proxies)} HTTP proxies from {self.cache_file}")
                        return ssl_proxies, http_proxies
                    else:
                        # Old format - load combined list
                        saved_proxies = data.get('working_proxies', [])
                        saved_proxies = self._deduplicate_list(saved_proxies)
                        
                        # Classify proxies into separate pools
                        ssl_proxies = [p for p in saved_proxies if self._is_ssl_capable_port(p)]
                        http_proxies = [p for p in saved_proxies if not self._is_ssl_capable_port(p)]
                        
                        logger.info(f"Loaded {len(saved_proxies)} saved proxies from {self.cache_file} (classified: {len(ssl_proxies)} SSL, {len(http_proxies)} HTTP)")
                        return ssl_proxies, http_proxies
            else:
                logger.info("No saved proxy file found")
                return [], []
        except Exception as e:
            logger.warning(f"Failed to load saved proxies: {e}")
            return [], []
    
    async def save_working_proxies(self, ssl_proxies: List[str], http_proxies: List[str]):
        """Save working proxies to file for future use with separate pools"""
        try:
            # Only create directory if the file has a directory path
            dir_path = os.path.dirname(self.cache_file)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
            
            # Deduplicate all pools before saving
            ssl_proxies = self._deduplicate_list(ssl_proxies)
            http_proxies = self._deduplicate_list(http_proxies)
            
            # Save separate pools for site-specific usage
            data = {
                'working_proxies': ssl_proxies + http_proxies,  # Combined for backward compatibility
                'ssl_proxies': ssl_proxies,          # SSL proxies for LinkedIn
                'http_proxies': http_proxies,        # HTTP proxies for Indeed
                'saved_at': time.strftime('%Y-%m-%d %H:%M:%S'),
                'count': len(ssl_proxies + http_proxies),
                'ssl_count': len(ssl_proxies),
                'http_count': len(http_proxies)
            }
            
            with open(self.cache_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            logger.info(f"Saved {len(ssl_proxies + http_proxies)} total proxies ({len(ssl_proxies)} SSL, {len(http_proxies)} HTTP) to {self.cache_file}")
        except Exception as e:
            logger.warning(f"Failed to save proxies: {e}")
    
    def _deduplicate_list(self, proxy_list: List[str]) -> List[str]:
        """Remove duplicates from proxy list while preserving order"""
        seen = set()
        result = []
        for proxy in proxy_list:
            if proxy not in seen:
                seen.add(proxy)
                result.append(proxy)
        return result
    
    def _is_ssl_capable_port(self, proxy: str) -> bool:
        """Check if proxy port suggests SSL capability - more inclusive approach"""
        # Common SSL-capable ports including standard proxy ports
        ssl_ports = [443, 8443, 8442, 9443, 10443, 11443, 12443, 8080, 3128, 1080, 8081, 8888]
        try:
            port = int(proxy.split(':')[1])
            return port in ssl_ports
        except:
            return False

class ProxyValidator:
    """Handles proxy validation with async operations"""
    
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.dead_proxies: Set[str] = set()
    
    async def validate_proxies_async(self, proxies: List[str]) -> Tuple[List[str], List[str]]:
        """Async validation of proxies, separating HTTP and SSL proxies"""
        if not proxies:
            return [], []
        
        if self.config.verbose or len(proxies) <= 10:
            print(f"🔍 Async validation of {len(proxies)} proxies...", flush=True)
        
        # Separate SSL-capable and regular proxies based on ssl_only setting
        if self.config.ssl_only:
            ssl_proxies = [p for p in proxies if self._is_ssl_capable_port(p)]
            regular_proxies = []
        else:
            ssl_proxies = [p for p in proxies if self._is_ssl_capable_port(p)]
            regular_proxies = [p for p in proxies if not self._is_ssl_capable_port(p)]
        
        if self.config.verbose:
            print(f"   🔒 SSL-capable proxies: {len(ssl_proxies)}")
            if not self.config.ssl_only:
                print(f"   🌐 Regular HTTP proxies: {len(regular_proxies)}")
        
        # Test proxies concurrently
        tasks = []
        
        # Test SSL-capable proxies first
        if ssl_proxies:
            for proxy in ssl_proxies:
                tasks.append(self._validate_ssl_proxy_async(proxy))
        
        # Test regular HTTP proxies if not in SSL-only mode
        if regular_proxies and not self.config.ssl_only:
            for proxy in regular_proxies:
                tasks.append(self._validate_http_proxy_async(proxy))
        
        # Execute all validations concurrently
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Process results
        working_ssl_proxies = []
        working_http_proxies = []
        
        ssl_index = 0
        http_index = 0
        
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                continue
            
            if i < len(ssl_proxies):
                if result:
                    working_ssl_proxies.append(ssl_proxies[ssl_index])
                    if self.config.verbose:
                        print(f"   ✅ {ssl_proxies[ssl_index]} (SSL) is working")
                else:
                    self.dead_proxies.add(ssl_proxies[ssl_index])
                    if self.config.verbose:
                        print(f"   ❌ {ssl_proxies[ssl_index]} (SSL) failed")
                ssl_index += 1
            else:
                if result:
                    working_http_proxies.append(regular_proxies[http_index])
                    if self.config.verbose:
                        print(f"   ✅ {regular_proxies[http_index]} (HTTP) is working")
                else:
                    self.dead_proxies.add(regular_proxies[http_index])
                    if self.config.verbose:
                        print(f"   ❌ {regular_proxies[http_index]} (HTTP) is dead")
                http_index += 1
        
        # Show summary
        all_working_proxies = working_ssl_proxies + working_http_proxies
        if len(all_working_proxies) > 0:
            print(f"📊 Async validation complete: {len(all_working_proxies)}/{len(proxies)} working", flush=True)
            print(f"   🔒 SSL proxies: {len(working_ssl_proxies)} (for LinkedIn)", flush=True)
            if not self.config.ssl_only:
                print(f"   🌐 HTTP proxies: {len(working_http_proxies)} (for Indeed)", flush=True)
        
        return working_ssl_proxies, working_http_proxies
    
    async def _validate_ssl_proxy_async(self, proxy: str) -> bool:
        """Async validation if a proxy supports SSL/HTTPS (for LinkedIn)"""
        try:
            proxy_url = f'http://{proxy}'
            
            # Test multiple reliable HTTPS URLs for better success rate
            test_urls = [
                'https://icanhazip.com',
                'https://checkip.amazonaws.com',
                'https://ipinfo.io/ip',
                'https://httpbin.org/ip'
            ]
            
            # First test: Can proxy handle HTTPS with SSL verification (real SSL test)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(ssl=True)  # Enable SSL verification
            ) as session:
                for url in test_urls:
                    try:
                        async with session.get(
                            url,
                            proxy=proxy_url,
                            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                            ssl=True  # Enable SSL verification for real SSL test
                        ) as response:
                            if response.status == 200:
                                return True
                    except:
                        continue
            
            # Fallback test: If strict SSL fails, test with relaxed SSL (still better than HTTP)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(ssl=False)  # Relaxed SSL for fallback
            ) as session:
                for url in test_urls:
                    try:
                        async with session.get(
                            url,
                            proxy=proxy_url,
                            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                            ssl=False  # Relaxed SSL verification
                        ) as response:
                            if response.status == 200:
                                return True
                    except:
                        continue
                return False
        except Exception:
            return False
    
    async def _validate_http_proxy_async(self, proxy: str) -> bool:
        """Async validation of a single HTTP proxy"""
        try:
            proxy_url = f'http://{proxy}'
            
            # Test with multiple reliable URLs for better success rate
            test_urls = [
                "http://icanhazip.com",
                "http://checkip.amazonaws.com", 
                "http://ipinfo.io/ip",
                "http://httpbin.org/ip"
            ]
            
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)  # Increased timeout
            ) as session:
                for url in test_urls:
                    try:
                        async with session.get(
                            url,
                            proxy=proxy_url,
                            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                        ) as response:
                            if response.status == 200:
                                return True
                    except:
                        continue
            
            return False
        except:
            return False
    
    def _validate_ssl_proxy(self, proxy: str) -> bool:
        """Synchronous validation if a proxy supports SSL/HTTPS (for LinkedIn)"""
        try:
            proxy_dict = {
                'http': f'http://{proxy}',
                'https': f'http://{proxy}'
            }
            
            # Test multiple reliable HTTPS URLs for better success rate
            test_urls = [
                'https://icanhazip.com',
                'https://checkip.amazonaws.com',
                'https://ipinfo.io/ip',
                'https://httpbin.org/ip'
            ]
            
            # First test: Can proxy handle HTTPS with SSL verification (real SSL test)
            for url in test_urls:
                try:
                    response = requests.get(
                        url,
                        proxies=proxy_dict,
                        timeout=15,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                        verify=True  # Enable SSL verification for real SSL test
                    )
                    if response.status_code == 200:
                        return True
                except:
                    continue
            
            # Fallback test: If strict SSL fails, test with relaxed SSL (still better than HTTP)
            for url in test_urls:
                try:
                    response = requests.get(
                        url,
                        proxies=proxy_dict,
                        timeout=15,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                        verify=False  # Relaxed SSL verification for fallback
                    )
                    if response.status_code == 200:
                        return True
                except:
                    continue
            
            return False
            
        except Exception as e:
            # SSL errors are expected for non-SSL proxies
            return False
    
    def _is_ssl_capable_port(self, proxy: str) -> bool:
        """Check if proxy port suggests SSL capability - more inclusive approach"""
        # Common SSL-capable ports including standard proxy ports
        ssl_ports = [443, 8443, 8442, 9443, 10443, 11443, 12443, 8080, 3128, 1080, 8081, 8888]
        try:
            port = int(proxy.split(':')[1])
            return port in ssl_ports
        except:
            return False

class ProxyFetcher:
    """Handles fetching proxies from online sources with async operations"""
    
    def __init__(self, config: ProxyConfig):
        self.config = config
        self.session = None
        
        # Proxy sources configuration
        self.proxy_sources_config = {
            "monosans": {
                "enabled": True,
                "url": "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
                "description": "High success rate, good SSL coverage"
            },
            "spys_one": {
                "enabled": True,
                "url": "https://spys.one/en/free-proxy-list/",
                "description": "High-quality proxies with SSL indicators (may be blocked)"
            },
            "proxyscrape_all": {
                "enabled": True,
                "url": "https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all&skip=0&limit=2000",
                "description": "All proxies (let our SSL filtering handle it)"
            },
            "proxyscrape_premium": {
                "enabled": True,
                "url": "https://api.proxyscrape.com/v4/free-proxy-list/get?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=elite,anonymous&skip=0&limit=1000",
                "description": "High-quality proxies"
            },
            "free_proxy_list": {
                "enabled": True,
                "url": "https://free-proxy-list.net/en/",
                "description": "Good SSL proxy ratio (3/300 = 1%)"
            },
            "freeproxy_world_https": {
                "enabled": True,
                "url": "https://www.freeproxy.world/?type=https&anonymity=&country=&speed=1800&port=&page=1",
                "description": "FreeProxy.world HTTPS proxies (may be blocked by Cloudflare)"
            },
            "freeproxy_world_http": {
                "enabled": True,
                "url": "https://www.freeproxy.world/?type=http&anonymity=&country=&speed=1800&port=&page=1",
                "description": "FreeProxy.world HTTP proxies (may be blocked by Cloudflare)"
            },
            "advanced_name": {
                "enabled": True,
                "url": "https://advanced.name/freeproxy/68df12417db3f",
                "description": "Advanced.name proxy list"
            },
            "thespeedx": {
                "enabled": True,
                "url": "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
                "description": "Large volume, test last"
            }
        }
        
        # Build active proxy sources list
        self.proxy_sources = []
        for source_name, config in self.proxy_sources_config.items():
            if config["enabled"]:
                self.proxy_sources.append(config["url"])
    
    def _rebuild_sources_list(self):
        """Rebuild the proxy sources list based on current configuration"""
        self.proxy_sources = []
        for source_name, config in self.proxy_sources_config.items():
            if config["enabled"]:
                self.proxy_sources.append(config["url"])
    
    async def fetch_new_proxies_async(self, validator: ProxyValidator) -> Tuple[List[str], List[str]]:
        """Fetch new proxies from online sources with async operations"""
        print("🌐 Fetching new proxies from online sources (async)...", flush=True)
        all_ssl_proxies = []
        all_http_proxies = []
        tested_sources = 0
        
        for source in self.proxy_sources:
            try:
                print(f"📡 Fetching from {self._get_source_name(source)}...", flush=True)
                
                # Fetch proxies from this source
                source_proxies = await self._fetch_from_source_async(source)
                
                if not source_proxies:
                    print(f"   ⚠️  No valid proxies from {self._get_source_name(source)}")
                    continue
                
                print(f"   ✅ Fetched {len(source_proxies)} proxies from {self._get_source_name(source)}")
                
                # Validate this source's proxies
                ssl_proxies, http_proxies = await validator.validate_proxies_async(source_proxies)
                all_ssl_proxies.extend(ssl_proxies)
                all_http_proxies.extend(http_proxies)
                
                # Show clean summary
                source_name = self._get_source_name(source)
                total_working = len(ssl_proxies) + len(http_proxies)
                print(f"   📊 {source_name}: {total_working}/{len(source_proxies)} working")
                
                # Check if we have enough working proxies
                total_working = len(all_ssl_proxies) + len(all_http_proxies)
                if total_working >= self.config.min_working_proxies:
                    print(f"🎯 Found {total_working} working proxies (minimum {self.config.min_working_proxies} reached) - continuing to build pool...")
                
                # Optional: Stop if we have a very large pool
                if total_working >= 20:
                    print(f"🎯 Found {total_working} working proxies (large pool achieved) - stopping")
                    break
                    
                tested_sources += 1
                
            except Exception as e:
                print(f"   ❌ Failed to fetch from {self._get_source_name(source)}: {e}")
                tested_sources += 1
                continue
        
        total_working = len(all_ssl_proxies) + len(all_http_proxies)
        print(f"📈 Final results: {total_working} working proxies from {tested_sources} sources tested")
        return all_ssl_proxies, all_http_proxies
    
    async def _fetch_from_source_async(self, source: str) -> List[str]:
        """Fetch proxies from a single source with async operations"""
        try:
            # Special handling for different sources
            if 'spys.one' in source:
                await asyncio.sleep(2)  # Add delay to look more human-like
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Referer': 'https://spys.one/en/'
                }
            elif 'freeproxy.world' in source:
                await asyncio.sleep(1)
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1'
                }
            else:
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=headers
            ) as session:
                async with session.get(source) as response:
                    if response.status == 403:
                        if 'freeproxy.world' in source:
                            print(f"   ⚠️  FreeProxy.world blocked by Cloudflare - skipping this source")
                            return []
                        else:
                            raise aiohttp.ClientError(f"HTTP 403: {source}")
                    
                    response.raise_for_status()
                    text = await response.text()
                    
                    # Parse proxies based on source
                    if 'free-proxy-list.net' in source:
                        return self._parse_html_proxies(text)
                    elif 'spys.one' in source:
                        return self._parse_spys_proxies(text)
                    elif 'advanced.name' in source:
                        return self._parse_advanced_name_proxies(text)
                    elif 'freeproxy.world' in source:
                        return await self._fetch_freeproxy_world_multiple_pages_async(source)
                    else:
                        return self._parse_text_proxies(text)
        
        except Exception as e:
            if "403" in str(e) or "Forbidden" in str(e) or "Cloudflare" in str(e):
                print(f"   ⚠️  Source blocked: {self._get_source_name(source)}")
                return []
            else:
                raise e
    
    async def _fetch_freeproxy_world_multiple_pages_async(self, base_url: str) -> List[str]:
        """Fetch proxies from multiple pages of FreeProxy.world with async operations"""
        all_proxies = []
        
        try:
            # Fetch main page
            print(f"   📄 Fetching main page from FreeProxy.world...")
            main_proxies = await self._fetch_single_page_async(base_url)
            all_proxies.extend(main_proxies)
            print(f"   ✅ Main page: {len(main_proxies)} proxies")
            
            # Fetch additional pages
            for page in range(2, self.config.max_pages_per_source + 1):
                page_url = self._construct_page_url(base_url, page)
                print(f"   📄 Fetching page {page} from FreeProxy.world...")
                
                try:
                    await asyncio.sleep(2)  # Respectful delay
                    page_proxies = await self._fetch_single_page_async(page_url)
                    
                    if not page_proxies:
                        print(f"   📄 No proxies on page {page}, stopping pagination")
                        break
                    
                    all_proxies.extend(page_proxies)
                    print(f"   ✅ Page {page}: {len(page_proxies)} proxies")
                    
                except Exception as e:
                    if "403" in str(e) or "Forbidden" in str(e):
                        print(f"   ⚠️  Rate limited on page {page}, stopping pagination")
                        break
                    else:
                        print(f"   ❌ Failed to fetch page {page}: {e}")
                        break
            
            # Remove duplicates while preserving order
            unique_proxies = []
            seen = set()
            for proxy in all_proxies:
                if proxy not in seen:
                    seen.add(proxy)
                    unique_proxies.append(proxy)
            
            print(f"   📊 Total unique proxies from {self.config.max_pages_per_source} pages: {len(unique_proxies)}")
            return unique_proxies
            
        except Exception as e:
            print(f"   ❌ Error fetching multiple pages: {e}")
            return []
    
    async def _fetch_single_page_async(self, url: str) -> List[str]:
        """Fetch proxies from a single page with async operations"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9',
                'Accept-Language': 'en-US,en;q=0.9',
                'Accept-Encoding': 'gzip, deflate, br',
                'DNT': '1',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1'
            }
            
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                headers=headers
            ) as session:
                async with session.get(url) as response:
                    if response.status == 403:
                        raise aiohttp.ClientError("HTTP 403: Rate limited")
                    
                    response.raise_for_status()
                    text = await response.text()
                    return self._parse_freeproxy_world_proxies(text)
        
        except Exception as e:
            if "403" in str(e) or "Forbidden" in str(e):
                raise aiohttp.ClientError("Rate limited")
            else:
                raise e
    
    def _construct_page_url(self, base_url: str, page: int) -> str:
        """Construct URL for a specific page"""
        if 'page=' in base_url:
            import re
            return re.sub(r'page=\d+', f'page={page}', base_url)
        elif '?' in base_url:
            return f"{base_url}&page={page}"
        else:
            return f"{base_url}?page={page}"
    
    def _get_source_name(self, source: str) -> str:
        """Get a friendly name for the proxy source"""
        if 'monosans' in source:
            return 'monosans'
        elif 'spys.one' in source:
            return 'SPYS.ONE'
        elif 'proxyscrape.com' in source:
            if 'anonymity=elite,anonymous' in source:
                return 'ProxyScrape-Premium'
            return 'ProxyScrape'
        elif 'TheSpeedX' in source:
            return 'TheSpeedX'
        elif 'free-proxy-list.net' in source:
            return 'free-proxy-list.net'
        elif 'freeproxy.world' in source:
            if 'type=https' in source:
                return 'FreeProxy.World-HTTPS'
            elif 'type=http' in source:
                return 'FreeProxy.World-HTTP'
            else:
                return 'FreeProxy.World'
        elif 'advanced.name' in source:
            return 'Advanced.name'
        else:
            return source.split('/')[-1]
    
    def _parse_html_proxies(self, html: str) -> List[str]:
        """Parse proxies from HTML (free-proxy-list.net)"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            table = soup.find('table', {'class': 'table table-striped table-bordered'})
            if not table:
                return []
            
            proxies = []
            for row in table.find_all('tr')[1:]:  # Skip header
                cells = row.find_all('td')
                if len(cells) >= 2:
                    ip = cells[0].get_text().strip()
                    port = cells[1].get_text().strip()
                    if ip and port and port.isdigit():
                        proxies.append(f"{ip}:{port}")
            
            return proxies
        except:
            return []
    
    def _parse_spys_proxies(self, html: str) -> List[str]:
        """Parse proxies from SPYS.ONE format"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            proxies = []
            
            # Look for proxy entries in the table
            for row in soup.find_all('tr'):
                cells = row.find_all('td')
                if len(cells) >= 2:
                    for i, cell in enumerate(cells[:3]):
                        text = cell.get_text().strip()
                        if '.' in text and ':' in text:
                            parts = text.split()
                            for part in parts:
                                if ':' in part and '.' in part and not part.startswith('http'):
                                    clean_part = part.split()[0] if ' ' in part else part
                                    if self._is_valid_format(clean_part):
                                        proxies.append(clean_part)
                                        break
            
            # Also try regex approach
            import re
            ip_port_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b'
            matches = re.findall(ip_port_pattern, html)
            for match in matches:
                if self._is_valid_format(match) and match not in proxies:
                    proxies.append(match)
            
            return proxies
        except:
            return []
    
    def _parse_advanced_name_proxies(self, text: str) -> List[str]:
        """Parse proxies from Advanced.name format"""
        try:
            proxies = []
            for proxy in text.strip().split():
                proxy = proxy.strip()
                if self._is_valid_format(proxy):
                    proxies.append(proxy)
            return proxies
        except:
            return []
    
    def _parse_freeproxy_world_proxies(self, html: str) -> List[str]:
        """Parse proxies from FreeProxy.world table format"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            proxies = []
            
            table = soup.find('table')
            if not table:
                return []
            
            rows = table.find_all('tr')[1:]  # Skip header row
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:
                    try:
                        ip = cells[0].get_text().strip()
                        port = cells[1].get_text().strip()
                        
                        if ip and port and port.isdigit():
                            proxy = f"{ip}:{port}"
                            if self._is_valid_format(proxy):
                                proxies.append(proxy)
                    except:
                        continue
            
            return proxies
        except:
            return []
    
    def _parse_text_proxies(self, text: str) -> List[str]:
        """Parse proxies from plain text format"""
        proxies = []
        for line in text.strip().split('\n'):
            line = line.strip()
            if line and ':' in line and not line.startswith('#'):
                proxy = line.split()[0] if ' ' in line else line
                if self._is_valid_format(proxy):
                    proxies.append(proxy)
        return proxies
    
    def _is_valid_format(self, proxy: str) -> bool:
        """Check if proxy string is in valid format"""
        try:
            if ':' not in proxy:
                return False
            ip, port = proxy.split(':', 1)
            if not ip or not port:
                return False
            port_num = int(port)
            return 1 <= port_num <= 65535
        except:
            return False

class ProxyRotator:
    """Handles proxy rotation with efficient data structures"""
    
    def __init__(self):
        self.ssl_proxies = deque()
        self.http_proxies = deque()
        self.current_ssl_index = 0
        self.current_http_index = 0
    
    def add_ssl_proxies(self, proxies: List[str]):
        """Add SSL proxies to rotation"""
        for proxy in proxies:
            if proxy not in self.ssl_proxies:
                self.ssl_proxies.append(proxy)
    
    def add_http_proxies(self, proxies: List[str]):
        """Add HTTP proxies to rotation"""
        for proxy in proxies:
            if proxy not in self.http_proxies:
                self.http_proxies.append(proxy)
    
    def get_next_ssl_proxy(self) -> Optional[str]:
        """Get next SSL-capable proxy for LinkedIn"""
        if not self.ssl_proxies:
            return None
        proxy = self.ssl_proxies[self.current_ssl_index]
        self.current_ssl_index = (self.current_ssl_index + 1) % len(self.ssl_proxies)
        return proxy
    
    def get_next_http_proxy(self) -> Optional[str]:
        """Get next HTTP proxy for Indeed"""
        if not self.http_proxies:
            return None
        proxy = self.http_proxies[self.current_http_index]
        self.current_http_index = (self.current_http_index + 1) % len(self.http_proxies)
        return proxy
    
    def get_next_proxy(self, site: str = None) -> Optional[str]:
        """Get next proxy in rotation, with site-specific selection"""
        if site == "linkedin":
            return self.get_next_ssl_proxy()
        elif site == "indeed":
            return self.get_next_http_proxy()
        else:
            # Fallback to SSL proxies first, then HTTP
            ssl_proxy = self.get_next_ssl_proxy()
            if ssl_proxy:
                return ssl_proxy
            return self.get_next_http_proxy()
    
    def mark_proxy_dead(self, proxy: str):
        """Mark proxy as dead and remove from rotation"""
        removed_from_any = False
        
        # Remove from SSL pool if present
        if proxy in self.ssl_proxies:
            self.ssl_proxies.remove(proxy)
            removed_from_any = True
            print(f"💀 Marked {proxy} as dead (SSL pool: {len(self.ssl_proxies)} remaining)")
            
            if self.current_ssl_index >= len(self.ssl_proxies):
                self.current_ssl_index = 0
        
        # Remove from HTTP pool if present
        if proxy in self.http_proxies:
            self.http_proxies.remove(proxy)
            removed_from_any = True
            print(f"💀 Marked {proxy} as dead (HTTP pool: {len(self.http_proxies)} remaining)")
            
            if self.current_http_index >= len(self.http_proxies):
                self.current_http_index = 0
        
        return removed_from_any
    
    def get_stats(self) -> Dict:
        """Get proxy statistics"""
        return {
            'ssl_proxies': len(self.ssl_proxies),
            'http_proxies': len(self.http_proxies),
            'total_proxies': len(self.ssl_proxies) + len(self.http_proxies),
            'current_ssl_index': self.current_ssl_index,
            'current_http_index': self.current_http_index
        }

class SmartProxyManager:
    """Smart proxy manager with modular architecture and async operations"""
    
    def __init__(self, config: ProxyConfig = None):
        """
        Initialize smart proxy manager with modular components
        
        Args:
            config: ProxyConfig instance with all configuration parameters
        """
        self.config = config or ProxyConfig()
        
        # Initialize modular components
        self.storage = ProxyStorage(self.config.proxy_cache_file)
        self.validator = ProxyValidator(self.config)
        self.fetcher = ProxyFetcher(self.config)
        self.rotator = ProxyRotator()
        
        # Legacy compatibility attributes
        self.working_proxies = []
        self.http_proxies = []
        self.ssl_proxies = []
        self.dead_proxies = set()
        self.current_proxy_index = 0
        self.current_http_index = 0
        self.current_ssl_index = 0
    
    def enable_source(self, source_name: str):
        """Enable a proxy source by name"""
        if source_name in self.fetcher.proxy_sources_config:
            self.fetcher.proxy_sources_config[source_name]["enabled"] = True
            self.fetcher._rebuild_sources_list()
            print(f"✅ Enabled source: {source_name}")
        else:
            print(f"❌ Source not found: {source_name}")
    
    def disable_source(self, source_name: str):
        """Disable a proxy source by name"""
        if source_name in self.fetcher.proxy_sources_config:
            self.fetcher.proxy_sources_config[source_name]["enabled"] = False
            self.fetcher._rebuild_sources_list()
            print(f"❌ Disabled source: {source_name}")
        else:
            print(f"❌ Source not found: {source_name}")
    
    def toggle_source(self, source_name: str):
        """Toggle a proxy source on/off"""
        if source_name in self.fetcher.proxy_sources_config:
            current_state = self.fetcher.proxy_sources_config[source_name]["enabled"]
            self.fetcher.proxy_sources_config[source_name]["enabled"] = not current_state
            self.fetcher._rebuild_sources_list()
            status = "enabled" if not current_state else "disabled"
            print(f"🔄 Toggled source: {source_name} -> {status}")
        else:
            print(f"❌ Source not found: {source_name}")
    
    def show_sources_status(self):
        """Show the current status of all proxy sources"""
        print("📊 Proxy Sources Configuration")
        print("=" * 50)
        for source_name, config in self.fetcher.proxy_sources_config.items():
            status = "✅ Enabled" if config["enabled"] else "❌ Disabled"
            print(f"{status} {source_name:20} - {config['description']}")
        print(f"\n📈 Total active sources: {len(self.fetcher.proxy_sources)}")
    
    def get_enabled_sources(self):
        """Get list of enabled source names"""
        return [name for name, config in self.fetcher.proxy_sources_config.items() if config["enabled"]]
    
    def get_disabled_sources(self):
        """Get list of disabled source names"""
        return [name for name, config in self.fetcher.proxy_sources_config.items() if not config["enabled"]]
    
    def _print(self, message: str, force: bool = False):
        """Print message only if verbose mode is enabled or force is True"""
        if self.config.verbose or force:
            print(message, flush=True)
    
    def _print_progress(self, message: str, end: str = "\r"):
        """Print progress message that can be overwritten"""
        print(f"\r{message}", end=end, flush=True)
    
    def _print_summary(self, title: str, working: int, total: int, source: str = ""):
        """Print a clean summary line"""
        if source:
            print(f"   📊 {source}: {working}/{total} working")
        else:
            print(f"   📊 {title}: {working}/{total} working")
    
    async def load_saved_proxies(self) -> List[str]:
        """Load previously saved working proxies from file with separate pool support"""
        ssl_proxies, http_proxies = await self.storage.load_saved_proxies()
        
        # Update rotator with loaded proxies
        self.rotator.add_ssl_proxies(ssl_proxies)
        self.rotator.add_http_proxies(http_proxies)
        
        # Update legacy attributes for backward compatibility
        self.ssl_proxies = list(self.rotator.ssl_proxies)
        self.http_proxies = list(self.rotator.http_proxies)
        self.working_proxies = self.ssl_proxies + self.http_proxies
        
        return self.working_proxies
    
    async def save_working_proxies(self, proxies: List[str] = None):
        """Save working proxies to file for future use with separate pools"""
        if proxies is not None:
            # Backward compatibility - classify proxies
            ssl_proxies = [p for p in proxies if self.storage._is_ssl_capable_port(p)]
            http_proxies = [p for p in proxies if not self.storage._is_ssl_capable_port(p)]
        else:
            # Use current rotator state
            ssl_proxies = list(self.rotator.ssl_proxies)
            http_proxies = list(self.rotator.http_proxies)
        
        await self.storage.save_working_proxies(ssl_proxies, http_proxies)
    
    async def quick_validate_proxies(self, proxies: List[str]) -> List[str]:
        """Async validation of proxies using the new modular validator"""
        if not proxies:
            return []
        
        # Use the new async validator
        working_ssl_proxies, working_http_proxies = await self.validator.validate_proxies_async(proxies)
        
        # Update rotator with working proxies
        self.rotator.add_ssl_proxies(working_ssl_proxies)
        self.rotator.add_http_proxies(working_http_proxies)
        
        # Update legacy attributes for backward compatibility
        self.ssl_proxies = list(self.rotator.ssl_proxies)
        self.http_proxies = list(self.rotator.http_proxies)
        self.working_proxies = self.ssl_proxies + self.http_proxies
        
        return self.working_proxies
    
    async def _quick_validate_proxies_silent(self, proxies: List[str]) -> List[str]:
        """Silently validate proxies without verbose output using async operations"""
        if not proxies:
            return []
        
        # Temporarily disable verbose mode for silent validation
        original_verbose = self.config.verbose
        self.config.verbose = False
        
        try:
            # Use the new async validator
            working_ssl_proxies, working_http_proxies = await self.validator.validate_proxies_async(proxies)
            
            # Update rotator with working proxies
            self.rotator.add_ssl_proxies(working_ssl_proxies)
            self.rotator.add_http_proxies(working_http_proxies)
            
            # Update legacy attributes for backward compatibility
            self.ssl_proxies = list(self.rotator.ssl_proxies)
            self.http_proxies = list(self.rotator.http_proxies)
            self.working_proxies = self.ssl_proxies + self.http_proxies
            
            return self.working_proxies
        finally:
            # Restore original verbose mode
            self.config.verbose = original_verbose
    
    def _validate_single_proxy(self, proxy: str) -> bool:
        """Validate a single proxy quickly"""
        try:
            proxy_dict = {
                'http': f'http://{proxy}',
                'https': f'http://{proxy}'
            }
            
            # Test with multiple reliable URLs for better success rate
            test_urls = [
                "http://icanhazip.com",
                "http://checkip.amazonaws.com",
                "http://ipinfo.io/ip",
                "http://httpbin.org/ip"
            ]
            
            for url in test_urls:
                try:
                    response = requests.get(
                        url,
                        proxies=proxy_dict,
                        timeout=15,  # Increased timeout
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                    )
                    if response.status_code == 200:
                        return True
                except:
                    continue
            
            return False
            
        except:
            return False
    
    def _validate_ssl_proxy(self, proxy: str) -> bool:
        """Validate if a proxy supports SSL/HTTPS (for LinkedIn)"""
        try:
            proxy_dict = {
                'http': f'http://{proxy}',
                'https': f'http://{proxy}'
            }
            
            # Test multiple reliable HTTPS URLs for better success rate
            test_urls = [
                'https://icanhazip.com',
                'https://checkip.amazonaws.com',
                'https://ipinfo.io/ip',
                'https://httpbin.org/ip'
            ]
            
            # First test: Can proxy handle HTTPS with SSL verification (real SSL test)
            for url in test_urls:
                try:
                    response = requests.get(
                        url,
                        proxies=proxy_dict,
                        timeout=15,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                        verify=True  # Enable SSL verification for real SSL test
                    )
                    if response.status_code == 200:
                        return True
                except:
                    continue
            
            # Fallback test: If strict SSL fails, test with relaxed SSL (still better than HTTP)
            for url in test_urls:
                try:
                    response = requests.get(
                        url,
                        proxies=proxy_dict,
                        timeout=15,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
                        verify=False  # Relaxed SSL verification for fallback
                    )
                    if response.status_code == 200:
                        return True
                except:
                    continue
            
            return False
            
        except Exception as e:
            # SSL errors are expected for non-SSL proxies
            return False
    
    def _is_ssl_capable_port(self, proxy: str) -> bool:
        """Check if proxy port suggests SSL capability - more inclusive approach"""
        # Common SSL-capable ports including standard proxy ports
        ssl_ports = [443, 8443, 8442, 9443, 10443, 11443, 12443, 8080, 3128, 1080, 8081, 8888]
        try:
            port = int(proxy.split(':')[1])
            return port in ssl_ports
        except:
            return False
    
    async def fetch_new_proxies(self) -> List[str]:
        """Fetch new proxies from online sources using async operations"""
        # Use the new async fetcher
        ssl_proxies, http_proxies = await self.fetcher.fetch_new_proxies_async(self.validator)
        
        # Update rotator with new proxies
        self.rotator.add_ssl_proxies(ssl_proxies)
        self.rotator.add_http_proxies(http_proxies)
        
        # Update legacy attributes for backward compatibility
        self.ssl_proxies = list(self.rotator.ssl_proxies)
        self.http_proxies = list(self.rotator.http_proxies)
        self.working_proxies = self.ssl_proxies + self.http_proxies
        
        return self.working_proxies
    
    def _get_source_name(self, source: str) -> str:
        """Get a friendly name for the proxy source"""
        if 'monosans' in source:
            if 'https.txt' in source:
                return 'monosans-https'
            return 'monosans'
        elif 'spys.one' in source:
            return 'SPYS.ONE'
        elif 'proxyscrape.com' in source:
            if 'anonymity=elite,anonymous' in source:
                return 'ProxyScrape-Premium'
            return 'ProxyScrape'
        elif 'TheSpeedX' in source:
            if 'https.txt' in source:
                return 'TheSpeedX-https'
            return 'TheSpeedX'
        elif 'free-proxy-list.net' in source:
            return 'free-proxy-list.net'
        elif 'freeproxy.world' in source:
            if 'type=https' in source:
                return 'FreeProxy.World-HTTPS'
            elif 'type=http' in source:
                return 'FreeProxy.World-HTTP'
            else:
                return 'FreeProxy.World'
        elif 'advanced.name' in source:
            return 'Advanced.name'
        else:
            return source.split('/')[-1]
    
    def _test_source_proxies(self, source_proxies: List[str], source_name: str) -> List[str]:
        """Test proxies from a single source with clean, minimal progress display"""
        working_proxies = []
        batch_size = 100
        # Increase batch limit for SSL-only mode since we're pre-filtering
        max_batches = 100 if self.ssl_only else 50
        
        # Pre-filter SSL ports in SSL-only mode for better efficiency
        if self.ssl_only:
            original_count = len(source_proxies)
            ssl_capable_proxies = [p for p in source_proxies if self._is_ssl_capable_port(p)]
            total_proxies = len(ssl_capable_proxies)
            source_proxies = ssl_capable_proxies  # Use filtered list
            print(f"   🔍 Testing {source_name} ({total_proxies} SSL-capable proxies from {original_count} total)...")
        else:
            total_proxies = len(source_proxies)
            print(f"   🔍 Testing {source_name} ({total_proxies} proxies)...")
        
        total_batches = min(max_batches, (total_proxies + batch_size - 1) // batch_size)
        
        for batch_num in range(max_batches):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(source_proxies))
            
            if start_idx >= len(source_proxies):
                break
                
            batch_proxies = source_proxies[start_idx:end_idx]
            
            # Test batch silently
            batch_working = self._quick_validate_proxies_silent(batch_proxies)
            working_proxies.extend(batch_working)
            
            # Show progress only every 10 batches or at key milestones
            if (batch_num + 1) % 10 == 0 or (batch_num + 1) == total_batches:
                progress_pct = int((batch_num + 1) / total_batches * 100)
                print(f"   📊 {source_name}: {progress_pct}% complete - {len(working_proxies)} working found")
            
            # Early exit if we have enough from this source
            if len(working_proxies) >= self.min_working_proxies:
                print(f"   🎯 {source_name}: Found {len(working_proxies)} working proxies (enough) - stopping")
                break
                
            # If we've tested all proxies from this source
            if end_idx >= len(source_proxies):
                print(f"   ✅ {source_name}: Complete - {len(working_proxies)} working from {total_proxies} tested")
                break
                
        return working_proxies
    
    def _parse_html_proxies(self, html: str) -> List[str]:
        """Parse proxies from HTML (free-proxy-list.net)"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            table = soup.find('table', {'class': 'table table-striped table-bordered'})
            if not table:
                return []
            
            proxies = []
            for row in table.find_all('tr')[1:]:  # Skip header
                cells = row.find_all('td')
                if len(cells) >= 2:
                    ip = cells[0].get_text().strip()
                    port = cells[1].get_text().strip()
                    if ip and port and port.isdigit():
                        proxies.append(f"{ip}:{port}")
            
            return proxies
        except:
            return []
    
    def _parse_spys_proxies(self, html: str) -> List[str]:
        """Parse proxies from SPYS.ONE format"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            proxies = []
            
            # Look for proxy entries in the table - SPYS.ONE has specific format
            for row in soup.find_all('tr'):
                cells = row.find_all('td')
                if len(cells) >= 2:
                    # Look for IP addresses in the first few cells
                    for i, cell in enumerate(cells[:3]):  # Check first 3 cells
                        text = cell.get_text().strip()
                        # Look for IP:port patterns
                        if '.' in text and ':' in text:
                            # Split by spaces and look for IP:port patterns
                            parts = text.split()
                            for part in parts:
                                if ':' in part and '.' in part and not part.startswith('http'):
                                    # Clean up the part (remove any extra characters)
                                    clean_part = part.split()[0] if ' ' in part else part
                                    if self._is_valid_format(clean_part):
                                        proxies.append(clean_part)
                                        break
            
            # Also try to extract from the raw text using regex-like approach
            import re
            # Look for IP:port patterns in the entire HTML
            ip_port_pattern = r'\b(?:\d{1,3}\.){3}\d{1,3}:\d{1,5}\b'
            matches = re.findall(ip_port_pattern, html)
            for match in matches:
                if self._is_valid_format(match) and match not in proxies:
                    proxies.append(match)
            
            return proxies
        except:
            return []
    
    def _parse_advanced_name_proxies(self, text: str) -> List[str]:
        """Parse proxies from Advanced.name format (space-separated IP:PORT list)"""
        try:
            proxies = []
            # Split by spaces and filter for valid IP:PORT format
            for proxy in text.strip().split():
                proxy = proxy.strip()
                if self._is_valid_format(proxy):
                    proxies.append(proxy)
            return proxies
        except:
            return []
    
    def _parse_freeproxy_world_proxies(self, html: str) -> List[str]:
        """Parse proxies from FreeProxy.world table format"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            proxies = []
            
            # Find the proxy table
            table = soup.find('table')
            if not table:
                return []
            
            # Parse table rows (skip header row)
            rows = table.find_all('tr')[1:]  # Skip header row
            
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:  # Need at least IP and Port columns
                    try:
                        ip = cells[0].get_text().strip()
                        port = cells[1].get_text().strip()
                        
                        # Validate IP and port
                        if ip and port and port.isdigit():
                            proxy = f"{ip}:{port}"
                            if self._is_valid_format(proxy):
                                proxies.append(proxy)
                    except:
                        continue
            
            return proxies
        except:
            return []
    
    def _fetch_freeproxy_world_multiple_pages(self, base_url: str, max_pages: int = 5) -> List[str]:
        """Fetch proxies from multiple pages of FreeProxy.world"""
        all_proxies = []
        
        try:
            # First, try to get the main page (page 1)
            self._print(f"   📄 Fetching main page from FreeProxy.world...")
            try:
                import time
                start_time = time.time()
                # Use special headers for FreeProxy.world
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.9',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'DNT': '1',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                    'Sec-Fetch-Dest': 'document',
                    'Sec-Fetch-Mode': 'navigate',
                    'Sec-Fetch-Site': 'none',
                    'Sec-Fetch-User': '?1',
                    'Cache-Control': 'max-age=0'
                }
                try:
                    response = self.session.get(base_url, timeout=30, headers=headers)
                    response.raise_for_status()
                    end_time = time.time()
                    self._print(f"   ⏱️  Main page response time: {end_time - start_time:.2f}s")
                except Exception as e:
                    if "403" in str(e) or "Forbidden" in str(e) or "Cloudflare" in str(e):
                        self._print(f"   ⚠️  FreeProxy.world blocked by Cloudflare - skipping")
                        return []
                    else:
                        raise e
                
                # Parse proxies from main page
                page_proxies = self._parse_freeproxy_world_proxies(response.text)
                all_proxies.extend(page_proxies)
                self._print(f"   ✅ Main page: {len(page_proxies)} proxies")
                
            except Exception as e:
                self._print(f"   ❌ Failed to fetch main page: {e}")
                return []
            
            # Then try to fetch additional pages
            for page in range(2, max_pages + 1):
                # Construct URL for each page - replace existing page parameter if present
                if 'page=' in base_url:
                    # Replace existing page parameter
                    import re
                    page_url = re.sub(r'page=\d+', f'page={page}', base_url)
                elif '?' in base_url:
                    page_url = f"{base_url}&page={page}"
                else:
                    page_url = f"{base_url}?page={page}"
                
                self._print(f"   📄 Fetching page {page} from FreeProxy.world...")
                
                try:
                    # Add longer delay for pagination requests
                    import time
                    time.sleep(2)  # Increased delay to be more respectful
                    
                    start_time = time.time()
                    # Use special headers for FreeProxy.world
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'DNT': '1',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                        'Sec-Fetch-Dest': 'document',
                        'Sec-Fetch-Mode': 'navigate',
                        'Sec-Fetch-Site': 'none',
                        'Sec-Fetch-User': '?1',
                        'Cache-Control': 'max-age=0'
                    }
                    try:
                        response = self.session.get(page_url, timeout=30, headers=headers)
                        end_time = time.time()
                        self._print(f"   ⏱️  Page {page} response time: {end_time - start_time:.2f}s")
                    except Exception as e:
                        if "403" in str(e) or "Forbidden" in str(e) or "Cloudflare" in str(e):
                            self._print(f"   ⚠️  FreeProxy.world blocked by Cloudflare on page {page}, stopping")
                            break
                        else:
                            raise e
                    
                    # Check if we got rate limited
                    if response.status_code == 403:
                        self._print(f"   ⚠️  Rate limited on page {page}, stopping pagination")
                        break
                    
                    response.raise_for_status()
                    
                    # Parse proxies from this page
                    page_proxies = self._parse_freeproxy_world_proxies(response.text)
                    all_proxies.extend(page_proxies)
                    
                    self._print(f"   ✅ Page {page}: {len(page_proxies)} proxies")
                    
                    # If no proxies found on this page, we've likely reached the end
                    if not page_proxies:
                        self._print(f"   📄 No proxies on page {page}, stopping pagination")
                        break
                    
                except Exception as e:
                    self._print(f"   ❌ Failed to fetch page {page}: {e}")
                    # If we get rate limited or other errors, stop trying more pages
                    if "403" in str(e) or "Forbidden" in str(e):
                        self._print(f"   ⚠️  Rate limited, stopping pagination")
                        break
                    break
            
            # Remove duplicates while preserving order
            unique_proxies = []
            seen = set()
            for proxy in all_proxies:
                if proxy not in seen:
                    seen.add(proxy)
                    unique_proxies.append(proxy)
            
            self._print(f"   📊 Total unique proxies from {max_pages} pages: {len(unique_proxies)}")
            return unique_proxies
            
        except Exception as e:
            self._print(f"   ❌ Error fetching multiple pages: {e}")
            return []
    
    def _is_valid_format(self, proxy: str) -> bool:
        """Check if proxy string is in valid format"""
        try:
            if ':' not in proxy:
                return False
            ip, port = proxy.split(':', 1)
            if not ip or not port:
                return False
            port_num = int(port)
            return 1 <= port_num <= 65535
        except:
            return False
    
    async def initialize_smart(self, interactive: bool = True) -> Dict:
        """
        Smart proxy initialization with user choice logic using async operations
        
        Returns:
            Dictionary with initialization results and user choices
        """
        print("🧠 Smart Proxy System Initialization (Async)", flush=True)
        print("=" * 50, flush=True)
        
        # Step 1: Load saved proxies
        saved_proxies = await self.load_saved_proxies()
        
        if saved_proxies:
            print(f"📁 Found {len(saved_proxies)} saved proxies")
            
            # Show when proxies were saved
            try:
                with open(self.config.proxy_cache_file, 'r') as f:
                    data = json.load(f)
                    if data.get('saved_at'):
                        print(f"   📅 Proxies saved at: {data['saved_at']}", flush=True)
            except:
                pass  # Don't fail if we can't read the timestamp
            
            # Step 2: Quick validation of saved proxies
            working_saved = await self.quick_validate_proxies(saved_proxies)
            
            if len(working_saved) >= self.config.min_working_proxies:
                # Enough working proxies - start immediately
                await self.save_working_proxies()
                print(f"✅ Ready to start! {len(working_saved)} working proxies available")
                return {
                    'status': 'ready',
                    'working_proxies': len(working_saved),
                    'source': 'saved',
                    'user_choice': None
                }
            
            elif len(working_saved) > 0:
                # Some working proxies - start with what we have, fetch more in background
                print(f"⚠️  Only {len(working_saved)} working proxies (need {self.config.min_working_proxies})")
                print("🔄 Starting with available proxies and fetching more in background...")
                
                await self.save_working_proxies()
                
                # Try to get at least the minimum before starting
                if len(self.working_proxies) < self.config.min_working_proxies:
                    print(f"🌐 Fetching more proxies to reach minimum... (have {len(self.working_proxies)}/{self.config.min_working_proxies})")
                    new_proxies = await self.fetch_new_proxies()
                    
                    if new_proxies:
                        await self.save_working_proxies()
                        print(f"✅ Added {len(new_proxies)} new proxies. Total: {len(self.working_proxies)}")
                
                if len(self.working_proxies) >= self.config.min_working_proxies:
                    print(f"✅ Ready to start! {len(self.working_proxies)} working proxies available")
                    print("🔄 Background proxy fetching will continue to build the pool...")
                    return {
                        'status': 'ready',
                        'working_proxies': len(self.working_proxies),
                        'source': 'mixed',
                        'user_choice': None
                    }
                else:
                    print(f"❌ Failed to get enough proxies. Only {len(self.working_proxies)} working (need {self.config.min_working_proxies})")
                    return {
                        'status': 'insufficient_proxies',
                        'working_proxies': len(self.working_proxies),
                        'source': 'mixed',
                        'user_choice': None
                    }
            
            else:
                # No working saved proxies
                print("❌ No working saved proxies found")
        else:
            print("📁 No saved proxies found")
        
        # Step 3: No working proxies - ask user
        print(f"\n🤔 No working proxies available (need {self.config.min_working_proxies})")
        print("Choose an option:")
        print("1. Continue without proxies (faster, but may get blocked)")
        print("2. Wait for proxy fetching (slower, but more reliable)")
        print("3. Exit and try again later")
        
        if not interactive:
            print("🤖 Non-interactive mode: Auto-selecting option 2 (fetch new proxies)")
            choice = '2'
        else:
            while True:
                try:
                    choice = input("\nEnter your choice (1/2/3): ").strip()
                    
                    if choice == '1':
                        print("🚀 Starting without proxies...")
                        return {
                            'status': 'no_proxies',
                            'working_proxies': 0,
                            'source': 'none',
                            'user_choice': 'no_proxies'
                        }
                    elif choice == '2':
                        print("⏳ Fetching new proxies (this may take a few minutes)...")
                        new_proxies = await self.fetch_new_proxies()
                        
                        if new_proxies:
                            await self.save_working_proxies()
                            print(f"✅ Found {len(new_proxies)} working proxies!")
                            return {
                                'status': 'ready',
                                'working_proxies': len(new_proxies),
                                'source': 'new',
                                'user_choice': 'wait'
                            }
                        else:
                            print("❌ No working proxies found after fetching")
                            print("Choose an option:")
                            print("1. Continue without proxies")
                            print("2. Exit and try again later")
                            
                            if not interactive:
                                print("🤖 Non-interactive mode: Auto-selecting option 1 (continue without proxies)")
                                final_choice = '1'
                            else:
                                while True:
                                    final_choice = input("Enter your choice (1/2): ").strip()
                                    if final_choice == '1':
                                        return {
                                            'status': 'no_proxies',
                                            'working_proxies': 0,
                                            'source': 'none',
                                            'user_choice': 'no_proxies_after_fetch'
                                        }
                                    elif final_choice == '2':
                                        return {
                                            'status': 'exit',
                                            'working_proxies': 0,
                                            'source': 'none',
                                            'user_choice': 'exit'
                                        }
                                    else:
                                        print("Invalid choice. Please enter 1 or 2.")
                    
                    elif choice == '3':
                        print("👋 Exiting...")
                        return {
                            'status': 'exit',
                            'working_proxies': 0,
                            'source': 'none',
                            'user_choice': 'exit'
                        }
                    
                    else:
                        print("Invalid choice. Please enter 1, 2, or 3.")
                        
                except KeyboardInterrupt:
                    print("\n👋 Exiting...")
                    return {
                        'status': 'exit',
                        'working_proxies': 0,
                        'source': 'none',
                        'user_choice': 'interrupt'
                    }
        
        # Handle the choice (both interactive and non-interactive)
        if choice == '1':
            print("🚀 Starting without proxies...")
            return {
                'status': 'no_proxies',
                'working_proxies': 0,
                'source': 'none',
                'user_choice': 'no_proxies'
            }
        elif choice == '2':
            print("⏳ Fetching new proxies (this may take a few minutes)...")
            new_proxies = await self.fetch_new_proxies()
            
            if new_proxies:
                await self.save_working_proxies()
                print(f"✅ Found {len(new_proxies)} working proxies!")
                return {
                    'status': 'ready',
                    'working_proxies': len(new_proxies),
                    'source': 'new',
                    'user_choice': 'wait'
                }
            else:
                print("❌ No working proxies found after fetching")
                print("Choose an option:")
                print("1. Continue without proxies")
                print("2. Exit and try again later")
                
                if not interactive:
                    print("🤖 Non-interactive mode: Auto-selecting option 1 (continue without proxies)")
                    final_choice = '1'
                else:
                    while True:
                        final_choice = input("Enter your choice (1/2): ").strip()
                        if final_choice == '1':
                            return {
                                'status': 'no_proxies',
                                'working_proxies': 0,
                                'source': 'none',
                                'user_choice': 'no_proxies_after_fetch'
                            }
                        elif final_choice == '2':
                            return {
                                'status': 'exit',
                                'working_proxies': 0,
                                'source': 'none',
                                'user_choice': 'exit'
                            }
                        else:
                            print("Invalid choice. Please enter 1 or 2.")
        elif choice == '3':
            print("👋 Exiting...")
            return {
                'status': 'exit',
                'working_proxies': 0,
                'source': 'none',
                'user_choice': 'exit'
            }
        
        # Fallback return (should never reach here)
        return {
            'status': 'error',
            'working_proxies': 0,
            'source': 'none',
            'user_choice': 'error'
        }
    
    def get_next_proxy(self, site: str = None) -> Optional[str]:
        """Get next proxy in rotation, with site-specific selection"""
        return self.rotator.get_next_proxy(site)
    
    def get_next_ssl_proxy(self) -> Optional[str]:
        """Get next SSL-capable proxy for LinkedIn"""
        return self.rotator.get_next_ssl_proxy()
    
    def get_next_http_proxy(self) -> Optional[str]:
        """Get next HTTP proxy for Indeed"""
        return self.rotator.get_next_http_proxy()
    
    async def mark_proxy_dead(self, proxy: str):
        """Mark proxy as dead and remove from rotation"""
        removed_from_any = self.rotator.mark_proxy_dead(proxy)
        
        if removed_from_any:
            self.dead_proxies.add(proxy)
            # Update legacy attributes for backward compatibility
            self.ssl_proxies = list(self.rotator.ssl_proxies)
            self.http_proxies = list(self.rotator.http_proxies)
            self.working_proxies = self.ssl_proxies + self.http_proxies
            
            # Save updated proxy lists to JSON file
            await self.save_working_proxies()
    
    def get_proxy_dict(self, proxy: str) -> Dict[str, str]:
        """Convert proxy string to requests format"""
        return {
            'http': f'http://{proxy}',
            'https': f'http://{proxy}'
        }
    
    def get_stats(self) -> Dict:
        """Get proxy statistics"""
        rotator_stats = self.rotator.get_stats()
        return {
            'working_proxies': rotator_stats['total_proxies'],
            'ssl_proxies': rotator_stats['ssl_proxies'],
            'http_proxies': rotator_stats['http_proxies'],
            'dead_proxies': len(self.dead_proxies),
            'current_index': self.current_proxy_index,
            'current_ssl_index': rotator_stats['current_ssl_index'],
            'current_http_index': rotator_stats['current_http_index']
        }
    
    async def maintain_proxy_pool(self, target_count: int = 15, min_count: int = 8):
        """Maintain proxy pool by fetching more proxies if needed using async operations"""
        if len(self.working_proxies) < min_count:
            print(f"🔄 Proxy pool low ({len(self.working_proxies)}/{min_count}), fetching more...", flush=True)
            new_proxies = await self.fetch_new_proxies()
            
            if new_proxies:
                await self.save_working_proxies()
                print(f"✅ Added {len(new_proxies)} new proxies. Total: {len(self.working_proxies)}", flush=True)
            else:
                print("⚠️  No new working proxies found", flush=True)
        
        return len(self.working_proxies)
    
    def start_background_maintenance(self, target_count: int = 15, min_count: int = 8, max_cycles: int = 10):
        """Start background proxy maintenance thread with async support"""
        import threading
        import time
        
        def maintenance_worker():
            cycle_count = 0
            while cycle_count < max_cycles:
                try:
                    time.sleep(30)  # Wait 30 seconds between maintenance cycles
                    cycle_count += 1
                    
                    current_count = len(self.working_proxies)
                    if current_count < min_count:
                        print(f"🔄 Background: Proxy pool low ({current_count}/{min_count}), fetching more...", flush=True)
                        # Run async maintenance in a new event loop
                        import asyncio
                        asyncio.run(self.maintain_proxy_pool(target_count, min_count))
                except Exception as e:
                    print(f"🔄 Background proxy maintenance error: {e}", flush=True)
                    time.sleep(60)  # Wait longer on error
            
            print("🔄 Background proxy maintenance completed", flush=True)
        
        # Start background thread
        maintenance_thread = threading.Thread(target=maintenance_worker, daemon=True)
        maintenance_thread.start()
        print("🔄 Background proxy maintenance started (limited duration)", flush=True)
        return maintenance_thread

# Global instance
smart_proxy_manager = None

def get_smart_proxy_manager(config: ProxyConfig = None) -> SmartProxyManager:
    """Get global smart proxy manager instance"""
    global smart_proxy_manager
    if smart_proxy_manager is None:
        smart_proxy_manager = SmartProxyManager(config)
    return smart_proxy_manager

async def initialize_smart_proxy_system(config: ProxyConfig = None) -> Dict:
    """Initialize smart proxy system with user choice logic using async operations"""
    manager = get_smart_proxy_manager(config)
    
    # Detect if we're in a non-interactive environment
    import sys
    interactive = sys.stdin.isatty()
    
    return await manager.initialize_smart(interactive=interactive)

def get_next_proxy(site: str = None) -> Optional[str]:
    """Get next proxy from smart manager with site-specific selection"""
    manager = get_smart_proxy_manager()
    return manager.get_next_proxy(site)

async def mark_proxy_dead(proxy: str):
    """Mark proxy as dead in smart manager"""
    manager = get_smart_proxy_manager()
    await manager.mark_proxy_dead(proxy)

def get_proxy_dict(proxy: str) -> Dict[str, str]:
    """Get proxy dict for requests"""
    manager = get_smart_proxy_manager()
    return manager.get_proxy_dict(proxy)

def get_proxy_stats() -> Dict:
    """Get proxy statistics - wrapper for consistency"""
    manager = get_smart_proxy_manager()
    return manager.get_stats()

async def maintain_proxy_pool(target_count: int = 15, min_count: int = 8):
    """Maintain proxy pool - wrapper for consistency"""
    manager = get_smart_proxy_manager()
    return await manager.maintain_proxy_pool(target_count, min_count)

def start_background_maintenance(target_count: int = 15, min_count: int = 8, max_cycles: int = 10):
    """Start background proxy maintenance - wrapper for consistency"""
    manager = get_smart_proxy_manager()
    return manager.start_background_maintenance(target_count, min_count, max_cycles)


# Standalone execution for updating proxy list
if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description='Smart Proxy Manager - Standalone Mode (Async)')
    parser.add_argument('--ssl-only', action='store_true', default=True,
                       help='Only test and use SSL-capable proxies (for LinkedIn) - DEFAULT: True')
    parser.add_argument('--no-ssl', action='store_true',
                       help='Disable SSL-only mode and test all proxies')
    parser.add_argument('--min-proxies', type=int, default=10,
                       help='Minimum working proxies needed (default: 10)')
    parser.add_argument('--timeout', type=int, default=3,
                       help='Proxy validation timeout in seconds (default: 3)')
    parser.add_argument('--workers', type=int, default=10,
                       help='Number of concurrent validation threads (default: 10)')
    parser.add_argument('--max-pages', type=int, default=10,
                       help='Maximum pages to fetch from paginated sources like FreeProxy.world (default: 10)')
    parser.add_argument('--show-sources', action='store_true',
                       help='Show current proxy sources configuration and exit')
    parser.add_argument('--enable-source', type=str, metavar='SOURCE_NAME',
                       help='Enable a specific proxy source (use --show-sources to see available names)')
    parser.add_argument('--disable-source', type=str, metavar='SOURCE_NAME',
                       help='Disable a specific proxy source (use --show-sources to see available names)')
    parser.add_argument('--disable-blocked', action='store_true',
                       help='Automatically disable sources that are often blocked (SPYS.ONE, FreeProxy.world)')
    
    args = parser.parse_args()
    
    print("🧠 Smart Proxy Manager - Standalone Mode (Async)")
    print("=" * 50)
    
    # Create configuration
    config = ProxyConfig(
        min_working_proxies=args.min_proxies,
        validation_timeout=args.timeout,
        max_workers=args.workers,
        ssl_only=args.ssl_only and not args.no_ssl,
        max_pages_per_source=args.max_pages
    )
    
    # Create manager with specified settings
    manager = SmartProxyManager(config)
    
    # Handle source management commands
    if args.show_sources:
        manager.show_sources_status()
        exit(0)
    
    if args.enable_source:
        manager.enable_source(args.enable_source)
        manager.show_sources_status()
        exit(0)
    
    if args.disable_source:
        manager.disable_source(args.disable_source)
        manager.show_sources_status()
        exit(0)
    
    if args.disable_blocked:
        print("🚫 Disabling sources that are often blocked...")
        manager.disable_source("spys_one")
        manager.disable_source("freeproxy_world_https")
        manager.disable_source("freeproxy_world_http")
        print("✅ Disabled SPYS.ONE and FreeProxy.world sources")
        manager.show_sources_status()
        exit(0)
    
    # Handle SSL mode logic
    ssl_mode = args.ssl_only and not args.no_ssl
    
    print(f"🔒 SSL-Only Mode: {'✅ Enabled' if ssl_mode else '❌ Disabled'}")
    print(f"📊 Min Working Proxies: {args.min_proxies}")
    print(f"⏱️  Validation Timeout: {args.timeout}s")
    print(f"🧵 Max Workers: {args.workers}")
    print(f"📄 Max Pages per Source: {args.max_pages}")
    print("=" * 50)
    
    # Initialize smart proxy system (non-interactive mode) using async
    async def main():
        result = await manager.initialize_smart(interactive=False)
        
        if result['status'] == 'ready':
            stats = manager.get_stats()
            print(f"\n✅ SUCCESS!")
            print(f"📊 Working Proxies: {stats['working_proxies']}")
            print(f"💀 Dead Proxies: {stats['dead_proxies']}")
            print(f"💾 Saved to: {manager.config.proxy_cache_file}")
            
            if manager.working_proxies:
                print(f"\n📋 Working Proxies:")
                for i, proxy in enumerate(manager.working_proxies, 1):
                    ssl_indicator = "🔒" if manager.storage._is_ssl_capable_port(proxy) else "🌐"
                    print(f"   {i:2d}. {ssl_indicator} {proxy}")
        else:
            print(f"\n❌ FAILED: {result['status']}")
            if 'error' in result:
                print(f"Error: {result['error']}")
    
    # Run the async main function
    asyncio.run(main())
