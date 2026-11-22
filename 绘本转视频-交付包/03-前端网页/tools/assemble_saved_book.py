"""Assemble a saved run from an explicit local manifest; never invokes model APIs."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from storybook.recovery import assemble_recovered_book
from storybook.settings import Settings


async def main(args):
    raw=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
    def progress(done,total):print(json.dumps({'rendered':done,'total':total}),flush=True)
    book=await assemble_recovered_book(Settings(),raw,args.output_id,
        allow_source_motion=args.allow_source_motion,progress=progress)
    print(json.dumps({'book_id':book['id'],'duration':book['duration'],'mode':book['mode'],
                      'pages':len(book['pages']),'scenes':len(book['scenes']),'review':book['review']['status']},ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',required=True,help='Private JSON with source, story/audio hashes and selected clip/quality paths')
    parser.add_argument('--output-id',required=True,help='New book ID; existing directories are never overwritten')
    parser.add_argument('--allow-source-motion',action='store_true',help='Explicitly allow and disclose original-page motion fallback')
    asyncio.run(main(parser.parse_args()))
