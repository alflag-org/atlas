use strict;
use warnings;
use Test::More;
use File::Temp qw(tempdir);
use JSON::PP qw(encode_json);
use Atlas::Context;

my $directory = tempdir(CLEANUP => 1);
local $ENV{ATLAS_CONTEXT_FILE} = "$directory/context.json";
my $payload = {
    version => 1,
    host => { id => 'test-host', site => 'example' },
    paths => { home => '/opt/atlas' },
    program => {
        name => 'inventory', root => '/srv/inventory',
        runtime => { type => 'perl', version => '5.40.0', environment => '/opt/atlas/environments/perl/inventory' },
    },
    command => { name => 'show', path => '/srv/inventory/commands/show.pl', type => 'perl' },
    execution => { run_id => 'run', parent_run_id => undef, operation_id => 'operation' },
    working_directory => '/srv',
};

sub write_context {
    my ($text) = @_;
    open my $file, '>:raw', $ENV{ATLAS_CONTEXT_FILE} or die $!;
    print {$file} $text;
    close $file or die $!;
}

write_context(encode_json($payload));
is_deeply(Atlas::Context->current, $payload, 'preserves the shared JSON contract including nulls');
$payload->{execution}{run_id} = 'next-run';
write_context(encode_json($payload));
is(Atlas::Context->current->{execution}{run_id}, 'next-run', 'does not cache run identity');
for my $case (
    ['[]', qr/must contain an object/],
    ['{"version":2}', qr/version must be 1/],
    ['{"version":1}', qr/host must be an object/],
    ['invalid json', qr/JSON|malformed/],
) {
    write_context($case->[0]);
    eval { Atlas::Context->current };
    like($@, $case->[1], 'rejects invalid context');
}
delete $payload->{host}{id};
write_context(encode_json($payload));
eval { Atlas::Context->current };
like($@, qr/field is required: id/, 'requires host identity');
unlink $ENV{ATLAS_CONTEXT_FILE} or die $!;
eval { Atlas::Context->current };
like($@, qr/context file not found/, 'missing context fails');
delete $ENV{ATLAS_CONTEXT_FILE};
eval { Atlas::Context->current };
like($@, qr/ATLAS_CONTEXT_FILE is required/, 'requires explicit context location');
done_testing;
