!> Second program, a tiny timing loop: fpm run bench
program bench
    use demo
    implicit none
    integer, parameter :: n = 200
    real(wp), allocatable :: a(:, :), c(:, :)
    integer(8) :: t0, t1, rate
    integer :: i
    a = identity(n) * 2.0_wp
    call system_clock(t0, rate)
    c = matmul_naive(a, a)
    call system_clock(t1)
    print '(a,i0,a,i0,a,f0.3,a)', "naive matmul ", n, "x", n, ": ", real(t1 - t0) / real(rate), " s"
    print '(a,f0.6)', "parallel_sum(10**7) = ", parallel_sum(10**7)
    print '(a,f0.1)', "trace(c)/n = ", sum([(c(i, i), i = 1, n)]) / n
end program bench
