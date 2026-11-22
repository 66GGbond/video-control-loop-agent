"""One-image Seedream adapter. No automatic provider retries or fallback."""
import base64
import binascii
import json
import os
import urllib.error
import urllib.request

PRO = 'doubao-seedream-5-0-pro-260628'
FLASH = 'doubao-seedream-5-0-flash-260915'
KEYS = {PRO:'SEEDREAM_PRO_API_KEY',FLASH:'SEEDREAM_FLASH_API_KEY'}
ENDPOINT = 'https://ark.cn-beijing.volces.com/api/v3/images/generations'


def generate(model, prompt, images):
    key=os.getenv(KEYS.get(model,''))
    if not key: raise ValueError('Image model is not configured')
    if not prompt.strip() or len(images)>10: raise ValueError('Invalid image generation inputs')
    body={'model':model,'prompt':prompt,'size':'2K','response_format':'b64_json','output_format':'png','stream':False,'watermark':True}
    if images: body['image']=images
    request=urllib.request.Request(ENDPOINT,data=json.dumps(body).encode(),headers={'Authorization':f'Bearer {key}','Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(request,timeout=240) as response:
            raw=response.read(40*1024*1024+1)
        if len(raw)>40*1024*1024: raise ValueError('Image response is too large')
        parsed=json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ValueError(f'Image generation rejected (HTTP {exc.code}); no replacement request was submitted') from exc
    except OSError as exc:
        raise RuntimeError('Generation status is uncertain. Check the Ark console before retrying; no automatic retry was submitted.') from exc
    data=parsed.get('data')
    if parsed.get('error') or not isinstance(data,list) or len(data)!=1 or data[0].get('error'):
        raise ValueError('Image provider returned no complete single-image result')
    try: image=base64.b64decode(data[0]['b64_json'],validate=True)
    except (KeyError,TypeError,binascii.Error) as exc: raise ValueError('Invalid image data returned by provider') from exc
    if not image.startswith(b'\x89PNG\r\n\x1a\n') or len(image)>28*1024*1024:
        raise ValueError('Provider returned an unsupported image')
    return image
