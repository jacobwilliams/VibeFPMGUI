!> A plain test without a framework: exit code 0 means it passed.
program check_matrix
    use demo, only: wp, identity, matmul_naive
    implicit none
    real(wp), allocatable :: a(:, :), c(:, :)
    a = identity(10) * 3.0_wp
    c = matmul_naive(a, identity(10))
    if (maxval(abs(c - a)) > 1.0e-12_wp) then
        print '(a)', "matmul_naive(A, I) /= A"
        error stop 1
    end if
    print '(a)', "matrix tests passed"
end program check_matrix
