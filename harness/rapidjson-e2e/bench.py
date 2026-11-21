"""Interleaved timing of the jsonbench builds (see setup.sh).

usage: python3 bench.py [--dir W] [--rounds N] [--seconds S] [--only base,sse2,...]

Each round runs every build once, in rotating order, over the whole corpus. The
table shows the median ns per run of each build and, for NVOL, the median of
its per-round ratio to each other build (negative = NVOL faster). With
--stat min: the fastest round of each build and the ratio of those minima.
"""
import argparse
import glob
import os
import statistics
import subprocess

ap = argparse.ArgumentParser()
ap.add_argument('--dir', default=os.path.join(os.environ.get('E2E', os.path.expanduser('~/e2e')), 'rapidjson'))
ap.add_argument('--rounds', type=int, default=21)
ap.add_argument('--seconds', type=float, default=0.1)
ap.add_argument('--builds', default='base,sse2,sse42,nvol')
ap.add_argument('--target', default='nvol', help='build the ratios are computed for')
ap.add_argument('--stat', choices=['median', 'min'], default='median',
                help='min: fastest round of each build (robust when other processes add load)')
args = ap.parse_args()

builds = args.builds.split(',')
files = sorted(glob.glob(os.path.join(args.dir, 'corpus', '*.json')))
times = {}   # (build, file, op) -> [ns per round]
for r in range(args.rounds):
    order = builds[r % len(builds):] + builds[:r % len(builds)]
    for b in order:
        out = subprocess.run([os.path.join(args.dir, f'jsonbench-{b}'), 'bench', str(args.seconds)] + files,
                             check=True, capture_output=True, text=True).stdout
        for line in out.splitlines():
            f, op, ns = line.split()
            times.setdefault((b, os.path.basename(f), op), []).append(float(ns))
    print(f'round {r + 1}/{args.rounds}', flush=True)

keys = sorted({(f, op) for (_, f, op) in times}, key=lambda k: (k[0], ['parse', 'parse_len', 'insitu', 'write', 'pretty'].index(k[1])))
head = ['file', 'op'] + [f'{b} us' for b in builds] + [f'{args.target} vs {b}' for b in builds if b != args.target]
print('| ' + ' | '.join(head) + ' |')
print('|' + '---|' * len(head))
for f, op in keys:
    row = [f.replace('.json', ''), op]
    pick = min if args.stat == 'min' else statistics.median
    row += [f'{pick(times[(b, f, op)]) / 1000:.1f}' for b in builds]
    for b in builds:
        if b == args.target:
            continue
        if args.stat == 'min':
            r = min(times[(args.target, f, op)]) / min(times[(b, f, op)])
        else:
            r = statistics.median([l / s for l, s in zip(times[(args.target, f, op)], times[(b, f, op)])])
        row.append(f'{(r - 1) * 100:+.1f} %')
    print('| ' + ' | '.join(row) + ' |')
