test_scripts = [ 'conftest.py', 'pytest.ini', 'test_sshfs.py',
                 'test_hostname_validation.py', 'test_daemonize_order.py',
                 'util.py' ]
# test_daemonize_order.py reads sshfs.c, so copy that along
custom_target('test_scripts', input: test_scripts + ['../sshfs.c'],
              output: test_scripts + ['sshfs.c'], build_by_default: true,
              command: ['cp', '-fPp',
                        '@INPUT@', meson.current_build_dir() ])

# Provide something helpful when running 'ninja test'
wrong_cmd = executable('wrong_command', 'wrong_command.c',
                       install: false)
test('wrong_cmd', wrong_cmd)
