#pragma once

#include <optional>

#include "cereal/gen/cpp/log.capnp.h"
#include "selfdrive/pandad/panda.h"

bool ev9_preinit_status_enabled(Panda *panda);
bool ev9_preinit_maybe_rearm_offroad(Panda *panda,
                                     const std::optional<PandaEv9LongPreinitStatus> &preinit_status,
                                     bool is_onroad, bool ignition);
void fill_ev9_long_preinit_status(cereal::PandaState::Ev9LongPreinitStatus::Builder &status,
                                  const std::optional<PandaEv9LongPreinitStatus> &preinit_status,
                                  bool resident);
