"""Benchmark corpus for rapidjson: real files plus variants that stress one scan.

usage: python3 mkcorpus.py OUT_DIR TWITTER_JSON CITM_JSON TEXT_FILE

  twitter.json, citm_catalog.json   as published (simdjson's jsonexamples)
  twitter_min.json                  no whitespace at all
  twitter_pretty.json               indented with 4 spaces: long whitespace runs
  citm_min.json                     no whitespace
  text_lines.json                   array of ~8 MiB of English lines: long strings
"""
import json
import shutil
import sys

out, twitter, citm, text = sys.argv[1:5]
shutil.copy(twitter, f'{out}/twitter.json')
shutil.copy(citm, f'{out}/citm_catalog.json')
t = json.load(open(twitter, encoding='utf8'))
c = json.load(open(citm, encoding='utf8'))
json.dump(t, open(f'{out}/twitter_min.json', 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))
json.dump(t, open(f'{out}/twitter_pretty.json', 'w', encoding='utf8'), ensure_ascii=False, indent=4)
json.dump(c, open(f'{out}/citm_min.json', 'w', encoding='utf8'), ensure_ascii=False, separators=(',', ':'))
lines, size = [], 0
with open(text, encoding='utf8', errors='replace') as f:
    for line in f:
        lines.append(line.rstrip('\n'))
        size += len(line)
        if size >= 8 << 20:
            break
json.dump(lines, open(f'{out}/text_lines.json', 'w', encoding='utf8'), ensure_ascii=False, indent=1)
print('corpus in', out)
