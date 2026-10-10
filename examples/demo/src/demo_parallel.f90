!> Uses OpenMP (from the openmp metapackage dependency).
module demo_parallel
    use demo_kinds, only: wp
    use omp_lib, only: omp_get_max_threads
    implicit none
    private
    public :: parallel_sum, max_threads

contains

    integer function max_threads()
        max_threads = omp_get_max_threads()
    end function max_threads

    real(wp) function parallel_sum(n)
        integer, intent(in) :: n
        integer :: i
        parallel_sum = 0.0_wp
        !$omp parallel do reduction(+:parallel_sum)
        do i = 1, n
            parallel_sum = parallel_sum + 1.0_wp / real(i, wp)**2
        end do
        !$omp end parallel do
    end function parallel_sum

end module demo_parallel
