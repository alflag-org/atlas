use strict;
use warnings;
use Test::More;
use File::Temp qw(tempdir);
use File::Path qw(make_path);
use File::Copy qw(copy);
use Cwd qw(abs_path getcwd);
use FindBin;
use IPC::Open3;
use Symbol qw(gensym);
use JSON::PP qw(encode_json decode_json);

my $original_directory = getcwd();
my $directory = tempdir(CLEANUP => 1);
local %ENV = %ENV;
$ENV{PYTHONPATH} = abs_path("$FindBin::Bin/../../python");
$ENV{ATLAS_HOME} = "$directory/atlas";
$ENV{ATLAS_ETC_DIR} = "$directory/etc";
$ENV{ATLAS_VAR_DIR} = "$directory/var";
delete @ENV{qw(ATLAS_CONFIG_FILE ATLAS_HOST_FILE ATLAS_RUNTIMES_DIR ATLAS_ENVIRONMENTS_DIR ATLAS_RUN_ID ATLAS_OPERATION_ID ATLAS_RUN_TEMP_DIR)};
make_path("$directory/etc", "$directory/program/commands");

sub write_file {
    my ($path, $content) = @_;
    open my $file, '>:raw', $path or die $!;
    print {$file} $content;
    close $file or die $!;
}
write_file("$directory/etc/host.yml", '{"version":1,"host":{"id":"sdk-test"}}');
write_file("$directory/etc/config.yml", encode_json({
    runtime => { perl => { executable => abs_path($^X) } },
    programs => { inventory => { root => "$directory/program", runtime => { type => 'perl' } } },
}));
write_file("$directory/program/commands/context.pl", <<'PROGRAM');
use Atlas::Context;
use JSON::PP qw(encode_json);
print encode_json(Atlas::Context->current);
PROGRAM

sub atlas {
    my $error = gensym;
    my $pid = open3(my $input, my $output, $error, $ENV{ATLAS_TEST_PYTHON} || 'python', '-m', 'atlas.cli', @_);
    close $input;
    local $/;
    my $stdout = <$output> // '';
    my $stderr = <$error> // '';
    waitpid $pid, 0;
    return ($? >> 8, $stdout, $stderr);
}

my ($code, $out, $err) = atlas('env', 'create', 'inventory');
is($code, 0, 'creates a real Perl environment') or diag $err;
my $target = "$directory/atlas/environments/perl/inventory";
my $build = "$directory/sdk";
make_path("$build/lib/Atlas");
copy("$FindBin::Bin/../Makefile.PL", "$build/Makefile.PL") or die $!;
copy("$FindBin::Bin/../lib/Atlas/Context.pm", "$build/lib/Atlas/Context.pm") or die $!;
chdir $build or die $!;
is(system($^X, 'Makefile.PL', "INSTALL_BASE=$target"), 0, 'configures SDK installation');
is(system('make', 'pure_install'), 0, 'installs SDK using MakeMaker');
($code, $out, $err) = atlas('run', 'context');
is($code, 0, 'executes installed Atlas::Context with selected interpreter') or diag $err;
my $context = decode_json($out);
is($context->{host}{id}, 'sdk-test', 'reads actual Atlas host identity');
is($context->{program}{runtime}{type}, 'perl', 'reads language-neutral runtime');
is($context->{program}{runtime}{environment}, $target, 'reads environment path');
is($context->{execution}{parent_run_id}, undef, 'top-level parent is null');
is($context->{execution}{run_id}, $context->{execution}{operation_id}, 'top-level operation follows run');
my $logs = "$directory/var/logs/runs.jsonl";
open my $file, '<', $logs or die $!;
my $record = decode_json(<$file>);
close $file;
is($record->{run_id}, $context->{execution}{run_id}, 'SDK and execution log share run identity');
chdir $original_directory or die $!;
done_testing;
