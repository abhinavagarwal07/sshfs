test_scripts = [ 'conftest.py', 'pytest.ini', 'test_sshfs.py',
                 'util.py', 'test_workloads.py', 'test_errors.py',
                 'test_stress.py' ]
custom_target('test_scripts', input: test_scripts,
              output: test_scripts, build_by_default: true,
              command: ['cp', '-fPp',
                        '@INPUT@', meson.current_build_dir() ])

fixture_files = [
    'fixtures/hello_c/main.c',
    'fixtures/hello_c/util.c',
    'fixtures/hello_c/util.h',
    'fixtures/hello_c/Makefile',
]
custom_target('test_fixtures', input: fixture_files,
              output: ['main.c', 'util.c', 'util.h', 'Makefile'],
              build_by_default: true,
              command: ['bash', '-c',
                        'mkdir -p ' + meson.current_build_dir() + '/fixtures/hello_c && ' +
                        'cp -fPp @INPUT@ ' + meson.current_build_dir() + '/fixtures/hello_c/'])

# Provide something helpful when running 'ninja test'
wrong_cmd = executable('wrong_command', 'wrong_command.c',
                       install: false)
test('wrong_cmd', wrong_cmd)
