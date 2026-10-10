!> Top-level library module that pulls the others together.
module demo
    use demo_kinds, only: wp
    use demo_utils, only: linspace, to_string
    use demo_stats, only: mean, variance
    use demo_matrix, only: matmul_naive, identity
    use demo_parallel, only: parallel_sum, max_threads
    use demo_legacy, only: legacy_scale
    use demo_broken, only: broken_answer
    use mathutils, only: clamp, golden_minimum
    implicit none
    private
    public :: wp, linspace, to_string, mean, variance, matmul_naive, identity, &
              parallel_sum, max_threads, legacy_scale, broken_answer, clamp, golden_minimum
end module demo
