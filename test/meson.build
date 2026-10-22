test_scripts = [ 'conftest.py', 'pytest.ini', 'test_sshfs.py',
                 'test_hostname_validation.py', 'test_reconnect_cache.py',
                 'test_stale_handles.py',
                 'util.py' ]
custom_target('test_scripts', input: test_scripts,
              output: test_scripts, build_by_default: true,
              command: ['cp', '-fPp',
                        '@INPUT@', meson.current_build_dir() ])

# Unit tests for cache.c.  The test #includes cache.c itself, so it is built
# from its own source only.  They need no mount and are in the 'unit' suite.
# Run them with 'meson test --suite unit': the wrong_cmd test below always
# fails, so a plain 'meson test' does too.
test_cache = executable('test_cache', 'test_cache.c',
                        include_directories: include_dirs,
                        dependencies: sshfs_deps,
                        c_args: ['-DFUSE_USE_VERSION=31'],
                        install: false)
test('cache', test_cache, suite: 'unit')

# Reconnect handling in cache.c.  Uses the public cache interface only, so
# cache.c is a normal source here.
cache_reconnect_unit = executable('cache_reconnect_unit',
                                  ['cache_reconnect_unit.c', '../cache.c'],
                                  include_directories: include_dirs,
                                  dependencies: sshfs_deps,
                                  c_args: ['-DFUSE_USE_VERSION=31'],
                                  install: false)
test('cache_reconnect', cache_reconnect_unit, suite: 'unit')

# Tests for the connection handling in sshfs.c.  The test #includes sshfs.c
# itself, so that is not listed as a source.
sshfs_unit_sources = ['sshfs_unit.c', '../cache.c']
if target_machine.system() == 'darwin'
  sshfs_unit_sources += [ '../compat/darwin_compat.c' ]
endif
sshfs_unit = executable('sshfs_unit', sshfs_unit_sources,
                        include_directories: include_dirs,
                        dependencies: sshfs_deps,
                        c_args: ['-DFUSE_USE_VERSION=31'],
                        install: false)
test('sshfs_conn', sshfs_unit, suite: 'unit')

# Provide something helpful when running 'ninja test'
wrong_cmd = executable('wrong_command', 'wrong_command.c',
                       install: false)
test('wrong_cmd', wrong_cmd)
