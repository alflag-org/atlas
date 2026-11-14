#include "EXTERN.h"
#include "perl.h"
#include "XSUB.h"

MODULE = AtlasTest::XS PACKAGE = AtlasTest::XS
PROTOTYPES: DISABLE

int
answer()
    CODE:
        RETVAL = 42;
    OUTPUT:
        RETVAL
