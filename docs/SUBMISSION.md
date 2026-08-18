# Submission metadata

Single source of truth for the draft form. `task.toml` and the draft are both
derived from this file. Never edit either independently.

Status: not written. Blocked on contracts.

Fields and bounds:
  title                    3-200
  workingSlug              3-80 lowercase-kebab
  collectionFamily         Library clone
  taskFamily               feature_development
  verifierFamily           programmatic
  objective                40-20000
  motivation               20-10000
  difficultyExplanation    40-20000
  expertTimeEstimateHours  descriptive, not a gate
  environmentSummary       40-20000
  resourceEstimate         cpuMillis, memoryMb, storageMb, gpuCount,
                           agentTimeoutSec (>= 7200), verifierTimeoutSec
  networkRequirements      none
  oracleStrategy           20-20000
  verificationStrategy     40-20000
  binarySuccessCondition   20-10000
  partialScoreStrategy     20-10000
  anticipatedExploits      20-20000
