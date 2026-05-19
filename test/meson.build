test_scripts = [ 'conftest.py', 'pytest.ini', 'test_sshfs.py',
                 'util.py', 'test_workloads.py', 'test_errors.py',
                 'test_stress.py' ]
custom_target('test_scripts', input: test_scripts,
              output: test_scripts, build_by_default: true,
              command: ['cp', '-fPp',
                        '@INPUT@', meson.current_build_dir() ])

# Symlink fixtures into the build directory so tests can find them.
run_command('bash', '-c',
            'ln -sfn ' + meson.current_source_dir() + '/fixtures ' +
            meson.current_build_dir() + '/fixtures',
            check: false)

# Provide something helpful when running 'ninja test'
wrong_cmd = executable('wrong_command', 'wrong_command.c',
                       install: false)
test('wrong_cmd', wrong_cmd)
