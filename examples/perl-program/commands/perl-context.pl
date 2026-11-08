use strict;
use warnings;
use Atlas::Context;

my $context = Atlas::Context->current;
print $context->{host}{id}, "\n";
print $context->{execution}{run_id}, "\n";
