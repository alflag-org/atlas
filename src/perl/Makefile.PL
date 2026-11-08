use strict;
use warnings;
use ExtUtils::MakeMaker;

WriteMakefile(
    NAME => 'Atlas::Context',
    VERSION_FROM => 'lib/Atlas/Context.pm',
    ABSTRACT => 'Read the Atlas execution context',
    AUTHOR => 'Atlas contributors',
    MIN_PERL_VERSION => '5.020',
    PREREQ_PM => { 'JSON::PP' => 0 },
    dist => { COMPRESS => 'gzip -9f' },
    TEST_REQUIRES => { 'Test::More' => 0 },
);
