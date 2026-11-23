"""Import an authorized PDF, finished narrated MP4 and SRT via a private manifest.

Uses the configured source-page vision model, but does not regenerate any
story, video or narration. The manifest contains file SHA256 fingerprints and
one stable interval per source page; gaps remain unattributed.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storybook.pipeline import assert_allowed_input
from storybook.providers import Provider
from storybook.settings import Settings
from storybook.video_import import import_video_book


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pdf', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--subtitles', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--book-id', required=True)
    parser.add_argument('--cached-only', action='store_true', help='只复用已验证读图缓存，缺页明确标注；不提交模型请求')
    args = parser.parse_args()
    manifest = assert_allowed_input(args.manifest)
    if manifest.stat().st_size > 1_000_000:
        parser.error('导入清单过大')
    raw = json.loads(manifest.read_text(encoding='utf-8-sig'))
    settings = Settings()
    provider = Provider(settings)
    try:
        book = await import_video_book(settings, provider, args.pdf, args.video, args.subtitles,
            raw, args.book_id, cached_only=args.cached_only,
            progress=lambda done, total: print(f'原书资料 {done}/{total}', flush=True))
        print(json.dumps({'id': book['id'], 'mode': book['mode'], 'pages': len(book['pages']),
                          'duration': book['duration'], 'review': book['review']['status']}, ensure_ascii=False))
    finally:
        await provider.close()


if __name__ == '__main__':
    asyncio.run(main())
