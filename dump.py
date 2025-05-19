import argparse
import asyncio
import os
import re
import sys
import time
from base64 import b64decode
from math import floor
from urllib.parse import urlparse

import requests
from aiohttp import ClientSession
from bs4 import BeautifulSoup
from tqdm import tqdm

BUNKR_VS_API_URL_FOR_SLUG = "https://bunkr.cr/api/vs"
SECRET_KEY_BASE = "SECRET_KEY_"


async def get_items_list(session: ClientSession, url: str, retries: int, extensions, only_export, custom_path=None):
    extensions_list = extensions.split(',') if extensions is not None else []

    async with session.get(url) as response:
        if response.status != 200:
            raise Exception(f"[-] HTTP error {response.status}")

        response_text = await response.text()
        soup = BeautifulSoup(response_text, 'html.parser')
        is_bunkr = "| Bunkr" in soup.find('title').text

        direct_link = False

        if is_bunkr:
            items = []
            soup = BeautifulSoup(response_text, 'html.parser')

            direct_link = soup.find('span', {'class': 'ic-videos'}) is not None or soup.find('div', {
                'class': 'lightgallery'}) is not None
            if direct_link:
                album_name = soup.find('h1', {'class': 'text-[20px]'})
                if album_name is None:
                    album_name = soup.find('h1', {'class': 'truncate'})

                album_name = remove_illegal_chars(album_name.text)
                item = await get_real_download_url(session, url, True)
                items.append(item)
            else:
                boxes = soup.find_all('a', {'class': 'after:absolute'})
                for box in boxes:
                    items.append({'url': box['href'], 'size': -1})

                album_name = soup.find('h1', {'class': 'truncate'}).text
                album_name = remove_illegal_chars(album_name)
        else:
            items = []
            items_dom = soup.find_all('a', {'class': 'image'})
            for item_dom in items_dom:
                items.append({'url': f"https://cyberdrop.me{item_dom['href']}", 'size': -1})
            album_name = remove_illegal_chars(soup.find('h1', {'id': 'title'}).text)

        download_path = get_and_prepare_download_path(custom_path, album_name)
        already_downloaded_url = get_already_downloaded_url(download_path)

        for item_index, item in enumerate(items):
            if not direct_link:
                orig_url = item['url']
                item = await get_real_download_url(session, item['url'], is_bunkr)
                if item['url'] == '/':
                    print(f"unable to find a download link for file https://bunkr.si{orig_url}")
                    continue
                if item is None:
                    print(f"[-] Unable to find a download link")

            extension = get_url_data(item['url'])['extension']
            if ((extension in extensions_list or len(extensions_list) == 0) and (
                    item['url'] not in already_downloaded_url)):
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


async def get_real_download_url(session: ClientSession, url, is_bunkr=True):
    if is_bunkr:
        url = url if 'https' in url else f'https://bunkr.si{url}'
    else:
        url = url.replace('/f/', '/api/f/')

    async with session.get(url) as response:
        if response.status != 200:
            print(f"[-] HTTP error {response.status} getting real url for {url}")
            return None

        if is_bunkr:
            slug = re.search(r'\/f\/(.*?)$', url).group(1)
            encryption_data = await get_encryption_data(session, slug)
            decrypted_url = decrypt_encrypted_url(encryption_data)
            return {'url': decrypted_url, 'size': -1}
        else:
            item_data = await response.json()
            return {'url': item_data['url'], 'size': -1, 'name': item_data['name']}


async def download(session: ClientSession, item_url, download_path, is_bunkr=False, file_name=None):
    file_name = get_url_data(item_url)['file_name'] if file_name is None else file_name
    final_path = os.path.join(download_path, file_name)

    async with session.get(item_url, timeout=5) as response:
        if response.status != 200:
            print(f"[-] Error downloading \"{file_name}\": {response.status}")
            return
        if response.url == "https://bnkr.b-cdn.net/maintenance.mp4":
            print(f"[-] Error downloading \"{file_name}\": Server is down for maintenance")

        file_size = int(response.headers.get('content-length', -1))
        with open(final_path, 'wb') as f:
            with tqdm(total=file_size, unit='iB', unit_scale=True, desc=file_name, leave=False) as pbar:
                async for chunk in response.content.iter_chunked(8192):
                    if chunk is not None:
                        f.write(chunk)
                        pbar.update(len(chunk))

    if is_bunkr and file_size > -1:
        downloaded_file_size = os.stat(final_path).st_size
        if downloaded_file_size != file_size:
            print(f"[-] {file_name} size check failed, file could be broken\n")
            return

    mark_as_downloaded(item_url, download_path)

    return


async def create_session():
    session = ClientSession(headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/133.0.0.0 Safari/537.36',
        'Referer': 'https://bunkr.si/',
    })
    return session


def get_url_data(url):
    parsed_url = urlparse(url)
    return {'file_name': os.path.basename(parsed_url.path), 'extension': os.path.splitext(parsed_url.path)[1],
            'hostname': parsed_url.hostname}


def get_and_prepare_download_path(custom_path, album_name):
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


def get_already_downloaded_url(download_path):
    file_path = os.path.join(download_path, 'already_downloaded.txt')

    if not os.path.isfile(file_path):
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read().splitlines()


def mark_as_downloaded(item_url, download_path):
    file_path = os.path.join(download_path, 'already_downloaded.txt')
    with open(file_path, 'a', encoding='utf-8') as f:
        f.write(f"{item_url}\n")

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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(sys.argv[1:])
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
        sys.exit(1)

    if args.u is not None and args.f is not None:
        print("[-] Please provide only one URL or file")
        sys.exit(1)

    loop = asyncio.get_event_loop()
    session = loop.run_until_complete(create_session())

    if args.f is not None:
        with open(args.f, 'r', encoding='utf-8') as f:
            urls = f.read().splitlines()
        for url in urls:
            print(f"[-] Processing \"{url}\"...")
            func = get_items_list(session, url, args.r, args.e, args.w, args.p)
            try:
                loop.run_until_complete(func)
                loop.run_until_complete(session.close())
            except KeyboardInterrupt:
                pass
        sys.exit(0)
    else:
        func = get_items_list(session, args.u, args.r, args.e, args.w, args.p)
        try:
            loop.run_until_complete(func)
            loop.run_until_complete(session.close())
        except KeyboardInterrupt:
            pass
    sys.exit(0)
