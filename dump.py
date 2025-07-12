import argparse
import asyncio
import os
import re
import sys
import time
from base64 import b64decode
from math import floor
from os import EX_OK
from urllib.parse import urlparse

import requests
from aiohttp import ClientSession, ClientTimeout
from bs4 import BeautifulSoup
from tqdm import tqdm

BUNKR_VS_API_URL_FOR_SLUG = "https://bunkr.cr/api/vs"
SECRET_KEY_BASE = "SECRET_KEY_"


async def get_items_list(session: ClientSession, url: str, retries: int, extensions, only_export, custom_path=""):
    extensions_list = extensions.split(',') if extensions is not None else []

    async with session.get(url) as response:
        if response.status != 200:
            raise Exception(f"[-] HTTP error {response.status}")

        response_text = await response.text()
        soup = BeautifulSoup(response_text, 'html.parser')
        is_bunkr = "| Bunkr" in soup.find('title').text

        isDirectLink = "/f/" in url
        albumName = None

        if is_bunkr:
            items = []
            if isDirectLink:
                albumName = soup.find('h1', {'class': 'truncate'}).strip()
                albumName = remove_illegal_chars(albumName.text)

                item = await get_real_download_url(session, url, True)
                items.append(item)
            else:
                itemLiks = soup.find_all('a', {'class': 'after:absolute'})
                for itemLink in itemLiks:
                    href:str = itemLink['href']
                    if href.startswith("/"):
                        # link is relative, make it absolute
                        responseUrl = response.url
                        href = f"{responseUrl.scheme}://{responseUrl.host}{href}"
                        items.append({'url': href, 'size': -1})

                albumName = soup.find('h1', {'class': 'truncate'}).text.strip()
                albumName = remove_illegal_chars(albumName)
        else:
            headers = {"referer": "https://cyberdrop.me/"}
            if isDirectLink:
                slug = re.search(r'/f/(.*?)$', url).group(1)
                async with session.get(f"https://api.cyberdrop.me/api/file/info/{slug}",
                                       headers=headers) as infoResponse:
                    infoJson = await infoResponse.json()
                    item = {"name": infoJson["name"], "size": infoJson["size"]}
                    authUrl = infoJson["auth_url"]
                    async with session.get(f"https://api.cyberdrop.me/api/file/auth/{slug}",
                                           headers=headers) as authResponse:
                        authJson = await authResponse.json()
                        item["url"] = authUrl
            items = []
            itemLinks = soup.find_all('a', {'class': 'image'})
            for itemLink in itemLinks:
                items.append({'url': f"https://cyberdrop.me{itemLink['href']}", 'size': -1})

            if not isDirectLink:
                albumName = remove_illegal_chars(soup.find('h1', {'id': 'title'}).text.strip())

        download_path = get_and_prepare_download_path(custom_path, albumName)
        already_downloaded_filenames = get_already_downloaded_filenames(download_path)

        for item_index, item in enumerate(items):
            if not isDirectLink:
                orig_url = item['url']
                item = await get_real_download_url(session, item['url'], is_bunkr)
                if item is None or item['url'] == '/':
                    print(f"unable to find a download link for file {orig_url}")
                    continue
                if item is None:
                    print(f"[-] Unable to find a download link")

            extension = get_url_data(item['url'])['extension']
            if ((extension in extensions_list or len(extensions_list) == 0) and (
                    item['name'] not in already_downloaded_filenames)):
                if only_export:
                    write_url_to_list(item['url'], download_path)
                else:
                    for i in range(1, retries + 1):
                        try:
                            print(f"[+] Downloading {item['url']} (try {i}/{retries})")
                            await download(session, item['url'], download_path, is_bunkr,
                                           item['name'] if not is_bunkr else None)
                            break
                        except requests.exceptions.ConnectionError as e:
                            if i < retries:
                                time.sleep(2)
                                pass
                            else:
                                raise e

        print(
            f"[+] File list exported in {os.path.join(download_path, 'url_list.txt')}" if only_export else f"[+] Download completed")


async def get_real_download_url(session: ClientSession, url, isBunkr=True):
    async with session.get(url) as response:
        if response.status != 200:
            print(f"[-] HTTP error {response.status} getting real url for {url}")
            return None

        slug = re.search(r'/f/(.*?)$', url).group(1)

        if isBunkr:
            encryption_data = await get_encryption_data(session, slug)
            decrypted_url = decrypt_encrypted_url(encryption_data)
            return {'url': decrypted_url, 'size': -1, 'name': slug}
        else:
            headers = {"Referer": "https://cyberdrop.me/"}
            item = {"url": None, "size": -1, "name": None}
            async with session.get(f"https://api.cyberdrop.me/api/file/info/{slug}", headers=headers) as infoResponse:
                if infoResponse.status != 200:
                    print(f"[-] HTTP error {response.status} getting real url for {url}")
                    return None
                infoJson = await infoResponse.json()
                item["name"] = infoJson["name"]
                item["size"] = infoJson["size"]
                authUrl = infoJson["auth_url"]
            async with session.get(f"https://api.cyberdrop.me/api/file/auth/{slug}", headers=headers) as authResponse:
                if infoResponse.status != 200:
                    print(f"[-] HTTP error {response.status} getting real url for {url}")
                    return None
                authJson = await authResponse.json()
                item["url"] = authJson["url"]

        return item


async def download(session: ClientSession, item_url, download_path, is_bunkr=False, filename=None):
    filename = get_url_data(item_url)['file_name'] if filename is None else filename
    final_path = os.path.join(download_path, filename)

    async with session.get(item_url) as response:
        if response.status != 200:
            print(f"[-] Error downloading \"{filename}\": {response.status}")
            return
        if response.url == "https://bnkr.b-cdn.net/maintenance.mp4":
            print(f"[-] Error downloading \"{filename}\": Server is down for maintenance")

        file_size = int(response.headers.get('content-length', -1))
        with open(final_path, 'wb') as f:
            with tqdm(total=file_size, unit='iB', unit_scale=True, desc=filename, leave=False) as pbar:
                async for chunk in response.content.iter_chunked(8192):
                    if chunk is not None:
                        f.write(chunk)
                        pbar.update(len(chunk))
                pbar.clear()

    if is_bunkr and file_size > -1:
        downloaded_file_size = os.stat(final_path).st_size
        if downloaded_file_size != file_size:
            print(f"[-] {filename} size check failed, file could be broken\n")
            return

    mark_as_downloaded(filename, download_path)

    return


async def createSession():
    session_timeout = ClientTimeout(total=None)
    session = ClientSession(headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://bunkr.si/',
    }, timeout=session_timeout)
    return session


def get_url_data(url):
    parsed_url = urlparse(url)
    return {'file_name': os.path.basename(parsed_url.path), 'extension': os.path.splitext(parsed_url.path)[1],
            'hostname': parsed_url.hostname}


def get_and_prepare_download_path(custom_path: str, album_name):
    final_path = 'downloads' if custom_path is None else custom_path
    final_path = os.path.join(final_path, album_name) if album_name is not None else 'downloads'
    final_path = final_path.replace('\n', '')

    if not os.path.isdir(final_path):
        os.makedirs(final_path)

    already_downloaded_path = os.path.join(final_path, 'already_downloaded.txt')
    if not os.path.isfile(already_downloaded_path):
        with open(already_downloaded_path, 'x', encoding='utf-8'):
            pass

    return final_path


def write_url_to_list(item_url, download_path):
    list_path = os.path.join(download_path, 'url_list.txt')

    with open(list_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

    return


def get_already_downloaded_filenames(download_path):
    file_path = os.path.join(download_path, 'already_downloaded.txt')

    if not os.path.isfile(file_path):
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read().splitlines()


def mark_as_downloaded(filename, download_path):
    file_path = os.path.join(download_path, 'already_downloaded.txt')
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(f"{filename}\n")

    return


def remove_illegal_chars(string):
    return re.sub(r'[<>:"/\\|?*\']|[\00-\031]', "-", string).strip()


async def get_encryption_data(session: ClientSession, slug=None):
    async with session.post(BUNKR_VS_API_URL_FOR_SLUG, json={'slug': slug}) as response:
        if response.status != 200:
            print(f"[-] HTTP ERROR {response.status} getting encryption data")
            return None

        return await response.json()


def decrypt_encrypted_url(encryption_data):
    secret_key = f"{SECRET_KEY_BASE}{floor(encryption_data['timestamp'] / 3600)}"
    encrypted_url_bytearray = list(b64decode(encryption_data['url']))
    secret_key_byte_array = list(secret_key.encode('utf-8'))

    decrypted_url = ""

    for i in range(len(encrypted_url_bytearray)):
        decrypted_url += chr(encrypted_url_bytearray[i] ^ secret_key_byte_array[i % len(secret_key_byte_array)])

    return decrypted_url


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-u", help="Url to fetch", type=str, required=False, default=None)
    parser.add_argument("-f", help="File to list of URLs to download", required=False, type=str, default=None)
    parser.add_argument("-r", help="Amount of retries in case the connection fails", type=int, required=False,
                        default=10)
    parser.add_argument("-e", help="Extensions to download (comma separated)", type=str)
    parser.add_argument("-p", help="Path to custom downloads folder")
    parser.add_argument("-w", help="Export url list (ex: for wget)", action="store_true")

    args = parser.parse_args()
    sys.stdout.reconfigure(encoding='utf-8')

    if args.u is None and args.f is None:
        print("[-] No URL or file provided")
        return 1

    if args.u is not None and args.f is not None:
        print("[-] Please provide only one URL or file")
        return 1

    session = await createSession()

    try:
        if args.f is not None:
            with open(args.f, 'r', encoding='utf-8') as f:
                urls = f.read().splitlines()
            for url in urls:
                print(f"[-] Processing \"{url}\"...")
                await get_items_list(session, url, args.r, args.e, args.w, args.p)
            sys.exit(0)
        else:
            await get_items_list(session, args.u, args.r, args.e, args.w, args.p)

    finally:
        await session.close()

    return EX_OK


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(EX_OK)
