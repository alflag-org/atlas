package Atlas::Context;

use strict;
use warnings;
use JSON::PP qw(decode_json);

our $VERSION = '0.01';

sub current {
    my ($class) = @_;
    my $path = $ENV{ATLAS_CONTEXT_FILE};
    die "ATLAS_CONTEXT_FILE is required\n" unless defined $path && length $path;
    die "context file not found: $path\n" unless -f $path && !-l $path;
    open my $file, '<:raw', $path or die "cannot open context file: $path\n";
    local $/;
    my $context = decode_json(<$file>);
    close $file or die "cannot close context file: $path\n";
    die "context file must contain an object\n" unless ref $context eq 'HASH';
    die "context version must be 1\n"
        unless defined $context->{version} && $context->{version} eq '1';
    for my $key (qw(host paths program command execution)) {
        die "context $key must be an object\n" unless ref $context->{$key} eq 'HASH';
    }
    die "context runtime must be an object\n"
        unless ref $context->{program}{runtime} eq 'HASH';
    for my $entry (
        [$context->{host}, 'id'],
        [$context->{program}, 'name'],
        [$context->{program}, 'root'],
        [$context->{program}{runtime}, 'type'],
        [$context->{command}, 'name'],
        [$context->{command}, 'path'],
        [$context->{command}, 'type'],
        [$context->{execution}, 'run_id'],
        [$context->{execution}, 'operation_id'],
        [$context, 'working_directory'],
    ) {
        my ($object, $key) = @$entry;
        die "context field is required: $key\n"
            unless defined $object->{$key} && !ref $object->{$key} && length $object->{$key};
    }
    return $context;
}

1;

__END__

=head1 NAME

Atlas::Context - read the Atlas JSON execution context

=head1 SYNOPSIS

    use Atlas::Context;
    my $context = Atlas::Context->current;
    print $context->{host}{id}, "\n";
    print $context->{execution}{run_id}, "\n";

=head1 DESCRIPTION

C<current> reads C<ATLAS_CONTEXT_FILE> on each call and returns an unblessed
hash reference. It requires a version 1 execution context and dies on a
missing, malformed, unsupported, or incomplete context. Outside Atlas,
provide a context file explicitly; no host configuration is read.

The context contains C<host>, C<paths>, C<program>, C<command>, C<execution>,
and C<working_directory>. C<execution> contains C<run_id>, nullable
C<parent_run_id>, and C<operation_id>. Program runtime fields are C<type>,
optional C<version>, and optional absolute C<environment>. JSON null becomes
Perl undef. Additional fields are preserved.

Programs and their OS account are trusted. This API is not a sandbox.

=cut
