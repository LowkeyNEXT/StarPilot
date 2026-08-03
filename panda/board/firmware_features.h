#pragma once

// Compile-time firmware feature selection. Vehicle-specific resident firmware
// layers include Panda's shared drivers and override only narrow policy hooks.
#ifdef PANDA_EV9_LONG_PREINIT
#include "board/ev9_long_preinit.h"
#endif
