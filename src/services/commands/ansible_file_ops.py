"""Remote Python snippets for file operations that shell regexes cannot preserve."""
import shlex

from .ansible_arguments import boolean, mode_argument


_LINEINFILE = r'''
import grp, os, pwd, re, shlex, shutil, stat, subprocess, sys, tempfile, time
keys = ('path','state','line','regexp','search_string','create','backrefs',
        'insertafter','insertbefore','firstmatch','validate','backup','mode','owner','group')
c = dict(zip(keys, sys.argv[1:]))
path = os.path.realpath(os.path.expanduser(c['path']))
present = c['state'] == 'present'
try:
    old_stat = os.stat(path)
    if not stat.S_ISREG(old_stat.st_mode):
        raise ValueError('lineinfile: destination is not a regular file')
    with open(path, 'rb') as handle:
        original = handle.read()
except FileNotFoundError:
    old_stat = None
    original = b''
    if not present:
        sys.exit(0)
    if c['create'] != '1':
        raise ValueError('lineinfile: destination does not exist; create is false')
lines = original.decode('utf-8', 'surrogateescape').splitlines(True)
regex = re.compile(c['regexp']) if c['regexp'] else None
def matches(line):
    if regex is not None:
        return bool(regex.search(line))
    if c['search_string']:
        return c['search_string'] in line
    return line.rstrip('\r\n') == c['line']
indices = [i for i, line in enumerate(lines) if matches(line)]
if not present:
    lines = [line for i, line in enumerate(lines) if i not in indices]
elif indices and (regex is not None or c['search_string']):
    index = indices[-1]
    value = regex.search(lines[index]).expand(c['line']) if c['backrefs'] == '1' else c['line']
    lines[index] = value + '\n'
elif c['backrefs'] != '1' and not any(line.rstrip('\r\n') == c['line'] for line in lines):
    before, after = c['insertbefore'], c['insertafter']
    index = len(lines)
    if before == 'BOF':
        index = 0
    elif before or (after and after != 'EOF'):
        insertion = re.compile(before or after)
        hits = [i for i, line in enumerate(lines) if insertion.search(line)]
        if hits:
            index = hits[0] if c['firstmatch'] == '1' else hits[-1]
            if not before:
                index += 1
    if index > 0 and lines[index - 1] and not lines[index - 1].endswith('\n'):
        lines[index - 1] += '\n'
    lines.insert(index, c['line'] + '\n')
content = ''.join(lines).encode('utf-8', 'surrogateescape')
def identity(value, lookup):
    return int(value) if value.isdigit() else lookup(value)[2]
def attributes(target, existing):
    current = os.stat(target)
    uid = identity(c['owner'], pwd.getpwnam) if c['owner'] else (existing.st_uid if existing else current.st_uid)
    gid = identity(c['group'], grp.getgrnam) if c['group'] else (existing.st_gid if existing else current.st_gid)
    if (uid, gid) != (current.st_uid, current.st_gid):
        os.chown(target, uid, gid)
    current_mode = stat.S_IMODE(os.stat(target).st_mode)
    if c['mode']:
        if not c['mode'].isdigit() or current_mode != int(c['mode'], 8):
            subprocess.run(['chmod', '--', c['mode'], target], check=True)
    elif existing:
        if current_mode != stat.S_IMODE(existing.st_mode):
            os.chmod(target, stat.S_IMODE(existing.st_mode))
    else:
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(target, 0o666 & ~mask)
if old_stat is not None and content == original:
    attributes(path, old_stat)
    sys.exit(0)
fd, temporary = tempfile.mkstemp(prefix='.dno-line-', dir=os.path.dirname(path))
try:
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    attributes(temporary, old_stat)
    if c['validate']:
        argv = [part.replace('%s', temporary) for part in shlex.split(c['validate'])]
        subprocess.run(argv, check=True)
    if old_stat is not None and c['backup'] == '1':
        shutil.copy2(path, path + '.' + str(os.getpid()) + '.' + time.strftime('%Y%m%d%H%M%S') + '~')
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
'''.strip()


def lineinfile_command(args):
    allowed = {'path','dest','destfile','name','state','line','value','regexp','regex',
               'search_string','create','backrefs','insertafter','insertbefore',
               'firstmatch','validate','backup','mode','owner','group'}
    unknown = args.keys() - allowed
    if unknown:
        return [], ["lineinfile: неподдержанные параметры: " + ", ".join(sorted(unknown))]
    path = args.get('path', args.get('dest', args.get('destfile', args.get('name'))))
    state = str(args.get('state', 'present'))
    line = args.get('line', args.get('value'))
    regexp = args.get('regexp', args.get('regex', ''))
    search = args.get('search_string', '')
    if not path or state not in ('present', 'absent'):
        return [], ["lineinfile: нужны path и state present/absent"]
    if state == 'present' and line is None:
        return [], ["lineinfile: нужен line"]
    if state == 'absent' and line is None and not regexp and not search:
        return [], ["lineinfile absent: нужен regexp/search_string/line"]
    if regexp and search:
        return [], ["lineinfile: regexp и search_string несовместимы"]
    if args.get('insertbefore') and args.get('insertafter'):
        return [], ["lineinfile: insertbefore и insertafter несовместимы"]
    backrefs = boolean(args.get('backrefs', False))
    if backrefs and (not regexp or search):
        return [], ["lineinfile: backrefs требует regexp"]
    validate = str(args.get('validate', ''))
    if validate and validate.count('%s') != 1:
        return [], ["lineinfile: validate должен содержать ровно один %s"]
    config = {'path': str(path), 'state': state, 'line': '' if line is None else str(line),
              'regexp': str(regexp), 'search_string': str(search),
              'create': boolean(args.get('create', False)), 'backrefs': backrefs,
              'insertafter': str(args.get('insertafter', '')),
              'insertbefore': str(args.get('insertbefore', '')),
              'firstmatch': boolean(args.get('firstmatch', False)), 'validate': validate,
              'backup': boolean(args.get('backup', False)),
              'mode': mode_argument(args['mode']) if args.get('mode') is not None else '',
              'owner': str(args.get('owner', '')), 'group': str(args.get('group', ''))}
    values = [str(int(v)) if isinstance(v, bool) else str(v) for v in config.values()]
    return ["python3 -c " + shlex.quote(_LINEINFILE) + " " + " ".join(map(shlex.quote, values))], [
        "lineinfile: для атомарной записи и проверки validate используется python3 на хосте"
    ]


STAT_SCRIPT = r'''
import json, os, stat, sys
path = os.path.expanduser(sys.argv[1])
follow = sys.argv[2] == '1'
try:
    info = os.stat(path) if follow else os.lstat(path)
except FileNotFoundError:
    result = {'exists': False}
else:
    result = {'exists': True, 'isdir': stat.S_ISDIR(info.st_mode),
              'isreg': stat.S_ISREG(info.st_mode), 'islnk': stat.S_ISLNK(info.st_mode),
              'mode': format(stat.S_IMODE(info.st_mode), '04o'),
              'size': info.st_size, 'uid': info.st_uid, 'gid': info.st_gid}
print(json.dumps(result))
'''.strip()
