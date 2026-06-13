test_scripts = [ 'conftest.py', 'pytest.ini', 'test_sshfs.py',
                 'test_hostname_validation.py',
                 'test_connection_resilience.py',
                 'util.py' ]
custom_target('test_scripts', input: test_scripts,
              output: test_scripts, build_by_default: true,
              command: ['cp', '-fPp',
                        '@INPUT@', meson.current_build_dir() ])

cache_reconnect_unit = executable('cache_reconnect_unit',
                                  ['cache_reconnect_unit.c', '../cache.c'],
                                  include_directories: include_directories('..'),
                                  dependencies: sshfs_deps,
                                  c_args: ['-DFUSE_USE_VERSION=31'],
                                  install: false)
test('cache_reconnect_unit', cache_reconnect_unit)

# Provide something helpful when running 'ninja test'
wrong_cmd = executable('wrong_command', 'wrong_command.c',
                       install: false)
test('wrong_cmd', wrong_cmd)
